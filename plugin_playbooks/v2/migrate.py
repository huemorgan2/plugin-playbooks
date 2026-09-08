"""plans/032 phase 12 — the migration comparison helper (master §3 P5).

"Reaches the same effects with the same args": the effect sequence of a
recorded v1 (pblang) run — its `playbook_step_runs` rows — against the
effect sequence of a v2 dry run's journal. Pure functions, no DB access:
callers pass rows / journal lists; `playbook_dry_run(compare=true)` wires
them (agent_tools.py `_dry_run`).

Effects are `tool / llm / agent / approve / wait_event / subtask` only —
`now` / `random` / `log` and the compute kinds (`code`, `condition`,
`parallel`, `loop`, `state`, `halt`) contribute nothing; a `parallel`
container's (v1) or a `ctx.gather` batch's (v2) members form an unordered
group compared as a multiset.

Canonical args (the equality the comparison uses): underscore-prefixed
effect options dropped (`_id`, `_timeout`, `_retry`); `vault:<name>` strings
kept literal (never resolved); `llm` / `agent` compare only `prompt`, cut to
2000 chars on both sides (v1 stored the rendered prompt truncated to 2000,
runner.py `_run_llm_step` / `_run_agent_step`); `subtask` compares only
`inputs`; `approve` / `wait_event` compare no args (v1 rows record none).
One more equality: a v1 arg is the Jinja-rendered STRING of the referenced
value (`{{ steps.fetch.result.rows }}` → `str(list)`), v2 passes the value
itself — a v1 string equal to `str(<v2 value>)` is the same arg.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

EFFECT_KINDS = ("tool", "llm", "agent", "approve", "wait_event", "subtask")
PROMPT_CUT = 2000

# v1 `StepKind` value → v2 effect kind (definition.py `StepKind`).
_V1_KIND = {
    "tool_call": "tool",
    "llm_step": "llm",
    "agent_step": "agent",
    "wait_for_approval": "approve",
    "wait_for_event": "wait_event",
    "subtask": "subtask",
}

# The "last green live run" rule (phase 12 Scope 3): newest run of the
# playbook with status `done`, not a test run, of the live version.
GREEN_LIVE_RUN_RULE = (
    "last green live run (status 'done', not a test run, of the live version)"
)


@dataclass
class Effect:
    kind: str
    name: str | None
    args: dict[str, Any] = field(default_factory=dict)
    occurrence: str = ""
    group: str | None = None


# ------------------------------------------------------------------ v1
def _as_dict(definition: Any) -> dict[str, Any]:
    if isinstance(definition, dict):
        return definition
    dump = getattr(definition, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True)
    return dict(definition or {})


def _walk_steps(
    steps: list[dict[str, Any]] | None, group: str | None,
    out: dict[str, tuple[dict[str, Any], str | None]],
) -> None:
    """`step id → (step dict, nearest enclosing parallel id)` over every
    nesting (condition branches, loop bodies, parallel branches)."""
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        sid = str(step.get("id"))
        out[sid] = (step, group)
        kind = step.get("kind")
        if kind == "parallel":
            for branch in step.get("branches") or []:
                _walk_steps(branch, sid, out)
        _walk_steps(step.get("then"), group, out)
        _walk_steps(step.get("else") or step.get("else_"), group, out)
        _walk_steps(step.get("body"), group, out)


def _row_get(row: Any, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _row_sort_key(row: Any) -> tuple:
    started = _row_get(row, "started_at")
    return (started is None, started or 0, str(_row_get(row, "id") or ""))


def v1_effects(definition: Any, rows: list[Any]) -> list[Effect]:
    """The effect sequence of a recorded v1 run from its step rows (ordered
    by `started_at` then primary key; one row per executed step, one per
    loop iteration). `definition` is the version row's definition (dict or
    `PlaybookDefinition`) — it names the tool / event / playbook of a step
    and the `parallel` container membership."""
    by_id: dict[str, tuple[dict[str, Any], str | None]] = {}
    _walk_steps(_as_dict(definition).get("steps"), None, by_id)
    seen: dict[str, int] = {}
    out: list[Effect] = []
    for row in sorted(rows, key=_row_sort_key):
        sid = str(_row_get(row, "step_id"))
        kind = _V1_KIND.get(str(_row_get(row, "step_kind")))
        seen[sid] = seen.get(sid, 0) + 1
        if kind is None:
            continue
        step, group = by_id.get(sid, ({}, None))
        inputs = _row_get(row, "inputs")
        inputs = inputs if isinstance(inputs, dict) else {}
        outputs = _row_get(row, "outputs")
        outputs = outputs if isinstance(outputs, dict) else {}
        name: str | None = None
        args: dict[str, Any] = {}
        if kind == "tool":
            name = outputs.get("tool") if isinstance(outputs.get("tool"), str) else step.get("tool")
            args = dict(inputs)
        elif kind in ("llm", "agent"):
            args = {"prompt": inputs.get("prompt")}
        elif kind == "wait_event":
            name = step.get("event") or outputs.get("event")
        elif kind == "subtask":
            name = step.get("playbook") or outputs.get("subtask")
            args = {"inputs": dict(inputs)}
        out.append(Effect(
            kind=kind, name=name, args=args, occurrence=f"{sid}#{seen[sid]}", group=group,
        ))
    return out


# ------------------------------------------------------------------ v2
def v2_groups(code: str) -> dict[str, str]:
    """`call-site id → gather container id` for every `ctx.gather` member of
    `code` (plugin/10 `build_graph`; static, like the v1 `parallel`
    membership read from the definition)."""
    from .checker import check
    from .graph import build_graph

    sites = check(code or "", name="migrate", version=0).summary.get("call_sites", [])
    graph = build_graph(code or "", name="migrate", version=None, triggers=None, call_sites=sites)
    groups: dict[str, str] = {}

    def _visit(block: dict[str, Any]) -> None:
        for item in block.get("items") or []:
            if item.get("kind") == "gather":
                for arg in item.get("args") or []:
                    cid = arg.get("call_site_id")
                    if cid:
                        groups[str(cid)] = str(item["node"])
            for key in ("then", "else", "body", "finally"):
                if item.get(key):
                    _visit(item[key])
            for h in item.get("handlers") or []:
                _visit(h["body"])

    _visit(graph.get("root") or {})
    return groups


def v2_effects(journal: list[dict[str, Any]], groups: dict[str, str] | None = None) -> list[Effect]:
    """The effect sequence of a v2 run from its journal (entry 0 = the run;
    `now` / `random` / `log` skipped). `groups` maps a call-site id to its
    gather container (`v2_groups(code)`)."""
    out: list[Effect] = []
    for e in journal:
        kind = e.get("kind")
        if kind not in EFFECT_KINDS or not e.get("id"):
            continue
        args = e.get("args") if isinstance(e.get("args"), dict) else {}
        name: str | None = None
        if kind in ("tool", "subtask"):
            name = e.get("name")
        elif kind == "wait_event":
            nm = args.get("name")
            name = nm if isinstance(nm, str) else None
        if kind in ("llm", "agent"):
            eff_args: dict[str, Any] = {"prompt": args.get("prompt")}
        elif kind == "subtask":
            inner = args.get("inputs")
            eff_args = {"inputs": dict(inner) if isinstance(inner, dict) else {}}
        elif kind in ("approve", "wait_event"):
            eff_args = {}
        else:
            eff_args = {k: v for k, v in args.items() if not str(k).startswith("_")}
        sid = str(e["id"])
        out.append(Effect(
            kind=str(kind), name=name, args=eff_args,
            occurrence=f"{sid}#{int(e.get('occurrence') or 1)}",
            group=(groups or {}).get(sid),
        ))
    return out


# ------------------------------------------------------------------ compare
def canonical_args(effect: Effect) -> dict[str, Any]:
    """Options dropped, prompt cut, vault refs untouched."""
    args = {k: v for k, v in (effect.args or {}).items() if not str(k).startswith("_")}
    if effect.kind in ("llm", "agent"):
        prompt = args.get("prompt")
        return {"prompt": prompt[:PROMPT_CUT] if isinstance(prompt, str) else prompt}
    return args


def _key(effect: Effect) -> tuple[str, str | None, str]:
    return (
        effect.kind, effect.name,
        json.dumps(canonical_args(effect), sort_keys=True, default=str),
    )


def _summary(effect: Effect) -> dict[str, Any]:
    d = asdict(effect)
    d["args"] = canonical_args(effect)
    return d


def _normalise_groups(effects: list[Effect]) -> list[Effect]:
    """Unordered groups compare as multisets: each maximal run of effects
    sharing one group is sorted by canonical key, so two orderings of the
    same batch line up position by position."""
    out: list[Effect] = []
    i = 0
    while i < len(effects):
        g = effects[i].group
        if g is None:
            out.append(effects[i])
            i += 1
            continue
        j = i
        while j < len(effects) and effects[j].group == g:
            j += 1
        out.extend(sorted(effects[i:j], key=_key))
        i = j
    return out


def _diff_paths(a: Any, b: Any, path: str, out: list[dict[str, Any]]) -> None:
    """JSON-pointer paths at which `a` and `b` differ, with both values."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}/{str(k).replace('~', '~0').replace('/', '~1')}"
            if k not in a:
                out.append({"path": p, "v1": None, "v2": b[k]})
            elif k not in b:
                out.append({"path": p, "v1": a[k], "v2": None})
            else:
                _diff_paths(a[k], b[k], p, out)
        return
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for n, (x, y) in enumerate(zip(a, b)):
            _diff_paths(x, y, f"{path}/{n}", out)
        return
    if isinstance(a, str) and not isinstance(b, str) and a == str(b):
        # v1 renders every templated arg through Jinja, i.e. `str(value)`
        # (runner.py `_render_template`); v2 passes the native value. The
        # rendered string of the v2 value IS the same arg.
        return
    if json.dumps(a, sort_keys=True, default=str) != json.dumps(b, sort_keys=True, default=str):
        out.append({"path": path or "/", "v1": a, "v2": b})


def compare_effects(v1: list[Effect], v2: list[Effect]) -> dict[str, Any]:
    """`{"match", "v1_count", "v2_count", "mismatches", "effects"}` — the six
    mismatch classes `order` (same multiset, different sequence; checked
    first and reported once), `missing` (v1 effect with no v2 counterpart),
    `extra` (v2 effect beyond v1), `kind`, `name`, `args` (with the JSON
    pointer paths that differ). Any mismatch ⇒ `match: false`."""
    a = _normalise_groups(list(v1))
    b = _normalise_groups(list(v2))
    keys_a = [_key(x) for x in a]
    keys_b = [_key(x) for x in b]
    mismatches: list[dict[str, Any]] = []
    if keys_a != keys_b and sorted(keys_a) == sorted(keys_b):
        pos = next(i for i, (x, y) in enumerate(zip(keys_a, keys_b)) if x != y)
        mismatches.append({
            "class": "order", "position": pos,
            "v1": [x.occurrence for x in a], "v2": [y.occurrence for y in b],
        })
    else:
        for pos in range(max(len(a), len(b))):
            if pos >= len(b):
                mismatches.append({
                    "class": "missing", "position": pos, "v1": _summary(a[pos]), "v2": None,
                })
                continue
            if pos >= len(a):
                mismatches.append({
                    "class": "extra", "position": pos, "v1": None, "v2": _summary(b[pos]),
                })
                continue
            x, y = a[pos], b[pos]
            if x.kind != y.kind:
                mismatches.append({
                    "class": "kind", "position": pos, "v1": _summary(x), "v2": _summary(y),
                })
                continue
            if x.name != y.name:
                mismatches.append({
                    "class": "name", "position": pos, "v1": _summary(x), "v2": _summary(y),
                })
                continue
            paths: list[dict[str, Any]] = []
            _diff_paths(canonical_args(x), canonical_args(y), "", paths)
            if paths:
                mismatches.append({
                    "class": "args", "position": pos, "v1": _summary(x), "v2": _summary(y),
                    "paths": paths,
                })
    return {
        "match": not mismatches,
        "v1_count": len(v1),
        "v2_count": len(v2),
        "mismatches": mismatches,
        "effects": {"v1": [_summary(x) for x in a], "v2": [_summary(y) for y in b]},
    }


# ------------------------------------------------------------------ rule
def require_green_live_run(
    run_status: str | None, is_test: bool, playbook_version: int | None,
    live_version: int | None,
) -> None:
    """Raise `ValueError` naming the rule when the compared run is not the
    kind of evidence the migration accepts."""
    problems: list[str] = []
    if run_status != "done":
        problems.append(f"status is '{run_status}', not 'done'")
    if is_test:
        problems.append("it is a test run (is_test / trigger agent-candidate)")
    if live_version is None or playbook_version != live_version:
        problems.append(
            f"it ran version {playbook_version}, the live version is {live_version}"
        )
    if problems:
        raise ValueError(
            f"compare needs the {GREEN_LIVE_RUN_RULE}; this run is not: "
            + "; ".join(problems) + "."
        )


__all__ = [
    "EFFECT_KINDS", "GREEN_LIVE_RUN_RULE", "PROMPT_CUT", "Effect", "canonical_args",
    "compare_effects", "require_green_live_run", "v1_effects", "v2_effects", "v2_groups",
]
