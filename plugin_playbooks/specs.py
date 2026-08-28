"""Playbook specs — fixture simulation with assertions (plans/002 phase 4).

A spec is a stored test for a playbook: fixture ``inputs``, scripted
``stubs`` for effectful steps, and ``expect`` assertions evaluated against
the dry-run trace. Specs auto-run on every candidate save and gate
``playbook_promote``.

``evaluate_spec`` is a pure function over (spec, dry-run result) — no DB, no
runner import — so the assertion semantics are unit-testable in isolation.
"""

from __future__ import annotations

import json
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError


class ToolExpect(BaseModel):
    model_config = {"extra": "forbid"}

    count: int | None = None
    # subset match on the RESOLVED args of every call to this tool: strings
    # match by substring, everything else by equality; dicts recurse.
    args_contain: dict[str, Any] | None = None


class SpecExpect(BaseModel):
    model_config = {"extra": "forbid"}

    status: str = Field(default="done", pattern="^(done|failed)$")
    steps_ran: list[str] | None = None       # exact execution order
    steps_not_ran: list[str] | None = None
    tool_calls: dict[str, ToolExpect] | None = None
    # step-id → substring that must appear in the step's trace output
    # (matched against the JSON serialization of the output).
    output_contains: dict[str, str] | None = None
    error_contains: str | None = None        # only meaningful with status: failed


class SpecDef(BaseModel):
    model_config = {"extra": "forbid"}

    description: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    # step-id or tool-name → scripted output. For tool_call steps the value
    # becomes the tool's `result`; for agent/llm steps it is the step output.
    stubs: dict[str, Any] = Field(default_factory=dict)
    expect: SpecExpect = Field(default_factory=SpecExpect)


def parse_spec_yaml(text: str) -> SpecDef:
    """Parse + validate a spec document. Raises ValueError with readable
    lines on bad YAML or schema violations."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"Spec is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("Spec must be a YAML mapping (keys: inputs, stubs, expect).")
    try:
        return SpecDef.model_validate(data)
    except ValidationError as e:
        lines = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in e.errors()
        ]
        raise ValueError("Spec is invalid: " + "; ".join(lines)) from e


def parse_spec_batch_yaml(text: str) -> tuple[dict[str, SpecDef], dict[str, str]]:
    """Parse a batch document — a YAML mapping of spec-name → spec body.

    Per-spec schema violations land in the errors dict under that spec's
    name and do NOT abort the sibling specs; only a document that isn't a
    mapping at the top level raises.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"Batch is not valid YAML: {e}") from e
    if not isinstance(data, dict) or not data:
        raise ValueError(
            "Batch must be a non-empty YAML mapping of spec-name → spec body "
            "(each body: inputs, stubs, expect)."
        )
    parsed: dict[str, SpecDef] = {}
    errors: dict[str, str] = {}
    for spec_name, body in data.items():
        key = str(spec_name)
        if not isinstance(body, dict):
            errors[key] = "Spec body must be a mapping (keys: inputs, stubs, expect)."
            continue
        try:
            parsed[key] = SpecDef.model_validate(body)
        except ValidationError as e:
            lines = [
                f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
                for err in e.errors()
            ]
            errors[key] = "Spec is invalid: " + "; ".join(lines)
    return parsed, errors


def _contains(expected: Any, actual: Any) -> bool:
    """Subset/substring match: dicts recurse per key, strings match by
    substring (against the string form of the actual value), everything else
    by equality."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(k in actual and _contains(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, str):
        if isinstance(actual, str):
            return expected in actual
        return expected in json.dumps(actual, default=str)
    return expected == actual


def evaluate_spec(spec: SpecDef, dry_result: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a spec's ``expect`` block against a dry-run result.

    Returns ``{passed: bool, failures: [str], checked: int}`` — each failure
    is a human-readable line naming what was expected and what was seen.
    """
    exp = spec.expect
    failures: list[str] = []
    checked = 0
    trace: list[dict[str, Any]] = dry_result.get("trace") or []

    checked += 1
    status = dry_result.get("status")
    if status != exp.status:
        err = dry_result.get("error")
        failures.append(
            f"expected status '{exp.status}', got '{status}'"
            + (f" (error: {err})" if err else "")
        )

    ran_order = [t.get("step_id") for t in trace]
    if exp.steps_ran is not None:
        checked += 1
        # compare against the order restricted to the steps the spec names —
        # unrelated steps in between are fine; wrong order/missing is not.
        seen = [s for s in ran_order if s in set(exp.steps_ran)]
        if seen != exp.steps_ran:
            failures.append(
                f"expected steps to run in order {exp.steps_ran}, saw {seen}"
            )
    if exp.steps_not_ran:
        for sid in exp.steps_not_ran:
            checked += 1
            if sid in ran_order:
                failures.append(f"expected step '{sid}' NOT to run, but it ran")

    if exp.tool_calls:
        for tool, texp in exp.tool_calls.items():
            calls = [
                t for t in trace
                if isinstance(t.get("output"), dict) and t["output"].get("tool") == tool
            ]
            if texp.count is not None:
                checked += 1
                if len(calls) != texp.count:
                    failures.append(
                        f"expected tool {tool} called {texp.count}x, saw {len(calls)}"
                    )
            if texp.args_contain is not None:
                checked += 1
                if not calls:
                    failures.append(
                        f"expected tool {tool} args to contain "
                        f"{texp.args_contain}, but it was never called"
                    )
                elif not any(
                    _contains(texp.args_contain, c["output"].get("resolved_args") or {})
                    for c in calls
                ):
                    seen_args = [c["output"].get("resolved_args") for c in calls]
                    failures.append(
                        f"expected tool {tool} args to contain "
                        f"{texp.args_contain}, saw {seen_args}"
                    )

    if exp.output_contains:
        by_id: dict[str, Any] = {}
        for t in trace:
            by_id[t.get("step_id")] = t.get("output")  # last write wins (loops)
        for sid, needle in exp.output_contains.items():
            checked += 1
            if sid not in by_id:
                failures.append(
                    f"expected step '{sid}' output to contain '{needle}', "
                    "but the step never ran"
                )
            elif not _contains(needle, by_id[sid]):
                failures.append(
                    f"expected step '{sid}' output to contain '{needle}', "
                    f"saw {json.dumps(by_id[sid], default=str)[:300]}"
                )

    if exp.error_contains is not None:
        checked += 1
        err = dry_result.get("error") or ""
        if exp.error_contains not in err:
            failures.append(
                f"expected error to contain '{exp.error_contains}', got '{err}'"
            )

    return {"passed": not failures, "failures": failures, "checked": checked}


def _stub_key_report(definition: Any, stubs: dict[str, Any]) -> tuple[list[str], set[str], set[str], bool]:
    """(unmatched stub keys, step ids, tool names, has_subtask) for a raw
    playbook definition dict. A stub key must be a step id or a tool name —
    anything else is silently ignored by the runner, which makes the spec
    test something other than what it claims."""
    ids: set[str] = set()
    tools: set[str] = set()
    has_subtask = False

    def walk(steps: Any) -> None:
        nonlocal has_subtask
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            if s.get("id"):
                ids.add(s["id"])
            if s.get("tool"):
                tools.add(s["tool"])
            if s.get("kind") == "subtask":
                has_subtask = True
            for key in ("then", "else", "body"):
                walk(s.get(key))
            for br in s.get("branches") or []:
                walk(br)

    if isinstance(definition, dict):
        walk(definition.get("steps"))
    unmatched = sorted(k for k in stubs if k not in ids and k not in tools)
    return unmatched, ids, tools, has_subtask


async def run_spec(runner: Any, playbook: Any, spec: SpecDef) -> dict[str, Any]:
    """Dry-run `playbook` (a real row or a candidate shim) with the spec's
    fixtures and evaluate the assertions."""
    dry = await runner.dry_run(playbook, inputs=spec.inputs, stubs=spec.stubs)
    result = evaluate_spec(spec, dry)
    # 0.14.2: a stub keyed by neither step id nor tool name never fires — the
    # dry-run substitutes {_dry: true} and downstream refs die with a message
    # blaming the playbook. Fail the spec loudly instead. Subtask playbooks
    # are exempt (stubs may target the sub-playbook's steps, unknowable here).
    unmatched, ids, tools, has_subtask = _stub_key_report(
        getattr(playbook, "definition", None), spec.stubs
    )
    if unmatched and not has_subtask:
        result["failures"] = [
            f"stub '{k}' matches no step id or tool name in this playbook — "
            f"it is never applied (step ids: {', '.join(sorted(ids)) or 'none'}; "
            f"tools: {', '.join(sorted(tools)) or 'none'})"
            for k in unmatched
        ] + result["failures"]
        result["passed"] = False
    return result


async def run_all_specs(
    session: Any,
    runner: Any,
    playbook_id: Any,
    target: Any,
    version_n: int,
    *,
    only_name: str | None = None,
) -> dict[str, Any]:
    """Run the stored specs of a playbook against `target` (the playbook row
    or a candidate shim) and update each spec row's last-result cache.

    Caller owns the session/transaction — commit after (the cache updates
    ride along). Shared by the candidate auto-run, playbook_spec_run, and
    the promote gate (tool + REST).
    """
    from sqlalchemy import select  # local: keep module import-light for tests

    from .models import PlaybookSpec
    from datetime import datetime, timezone

    q = select(PlaybookSpec).where(PlaybookSpec.playbook_id == playbook_id)
    if only_name:
        q = q.where(PlaybookSpec.name == only_name)
    rows = (await session.execute(q.order_by(PlaybookSpec.name))).scalars().all()
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            spec = SpecDef.model_validate(row.spec)
            res = await run_spec(runner, target, spec)
        except Exception as e:  # noqa: BLE001 — a broken spec is a failing spec
            res = {"passed": False, "failures": [f"spec could not run: {e}"], "checked": 0}
        row.last_result = res
        row.last_run_at = datetime.now(timezone.utc)
        row.last_version = version_n
        results.append({"spec": row.name, **res})
    failed = [r for r in results if not r["passed"]]
    return {
        "total": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


def spec_from_run(
    run: Any,
    step_rows: list[Any],
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Record & replay: build a spec document from a completed run.

    ``stubs`` come from the recorded tool-step outputs (keyed by step id;
    the tool result payload, not the wrapper), ``inputs`` from the run row,
    ``expect`` seeded with the run status, the execution order, and per-tool
    call counts. The result is a PROPOSAL for the agent/user to trim.
    """
    step_kinds = {
        s.get("id"): s.get("kind")
        for s in (definition.get("steps") or [])
    }
    stubs: dict[str, Any] = {}
    order: list[str] = []
    tool_counts: dict[str, int] = {}
    for row in step_rows:
        if row.step_id not in order:
            order.append(row.step_id)
        out = row.outputs
        if isinstance(out, dict) and out.get("tool"):
            tool_counts[out["tool"]] = tool_counts.get(out["tool"], 0) + 1
            stubs[row.step_id] = out.get("result")
        elif step_kinds.get(row.step_id) in ("agent_step", "llm_step") and out is not None:
            stubs[row.step_id] = out
    spec_doc: dict[str, Any] = {
        "description": f"pinned from run {run.id} ({run.started_at:%Y-%m-%d})",
        "inputs": run.inputs or {},
        "stubs": stubs,
        "expect": {
            "status": "done" if run.status == "done" else "failed",
            "steps_ran": order,
            "tool_calls": {
                tool: {"count": n} for tool, n in sorted(tool_counts.items())
            },
        },
    }
    if not spec_doc["expect"]["tool_calls"]:
        del spec_doc["expect"]["tool_calls"]
    return spec_doc
