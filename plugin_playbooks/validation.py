"""Playbook validation — the static "compiler" for playbook definitions.

`validate_definition` runs WITHOUT executing anything and returns ALL issues at
once (like a typechecker): schema errors, unknown step-config keys, missing
required fields, undefined `{{inputs.*}}` / `{{steps.<id>.*}}` references,
use-before-define, bad loop expressions, unknown tools, subtask cycles, trigger
errors, and a best-effort context-economy lint (007.009 Goal 6).

This is the gate behind `playbook_validate`, `playbook_save`, `playbook_propose`,
`playbook_edit`, and the REST create route. It never raises on a bad playbook —
it reports. Only genuinely broken *inputs* (non-dict) raise.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from jinja2 import Environment, TemplateSyntaxError, meta
from pydantic import ValidationError

from .definition import PlaybookDef, StepDef, StepKind, detect_subtask_cycles

# --- shared key tables (single source of truth; agent_tools imports these) ---

COMMON_STEP_KEYS = {"id", "kind", "explanation", "retry", "on_error", "timeout"}
KIND_KEYS: dict[str, set[str]] = {
    "tool_call": {"tool", "args"},
    "agent_step": {"prompt", "output_schema", "tools"},
    "llm_step": {"prompt", "output_schema", "purpose", "model", "system"},
    "condition": {"when", "then", "else", "else_"},
    "parallel": {"branches", "fan_in"},
    "wait_for_approval": {"show"},
    "wait_for_event": {"event", "event_filter", "timeout_seconds"},
    "subtask": {"playbook", "inputs_map", "returns"},
    "loop": {
        "over", "body", "max_iterations", "until", "while", "break_when",
        "concurrency", "item_name", "count", "collect",
    },
    # 007.009.01
    "state": {"state"},
    "halt": {"when", "value"},
}

# 007.009.01: the documented output shape per step kind. The bad-ref-shape
# check verifies a `steps.<id>.<field>` reference against the known keys for
# that step's kind — catching e.g. `.output` on a schemaless llm_step (the
# review-digest bug: a schemaless llm_step returns `_raw`, never `output`).
KIND_OUTPUT_KEYS: dict[str, set[str]] = {
    "tool_call": {"tool", "result"},
    "loop": {"iterations", "results", "collected", "stopped", "state_timeline",
             "_item", "_index"},
    "condition": {"branch", "condition"},
    "parallel": {"branches"},
    "state": {"ops"},
    "subtask": {"subtask_run_id", "status", "subtask", "steps"},
    "halt": {"halted", "value"},
    # agent_step / llm_step are schema-dependent — handled specially.
}

# Jinja globals available in the runner's SandboxedEnvironment. Anything else
# used as a bare callable/variable root is flagged.
_BUILTINS = {"range", "dict", "namespace", "cycler", "joiner", "lipsum"}

# Field names that strongly imply "a whole RAW collection" — used by the
# context-economy lint to spot a single LLM call that would dump everything.
# NB: `.collected` is deliberately excluded — that's the REDUCED per-item set
# (the correct pattern), fine to summarize in one step. (Matched via
# `_DEEP_COLLECTION_REF` below so it also catches `.result.messages`.)

_STEP_REF = re.compile(r"\bsteps\.([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?")
_INPUT_REF = re.compile(r"\binputs\.([A-Za-z_][A-Za-z0-9_]*)")
# context-economy: a collection hint word as the LAST attr of a step ref at any
# depth (catches both `steps.fetch.messages` and `steps.fetch.result.messages`).
_DEEP_COLLECTION_REF = re.compile(
    r"\bsteps\.[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*?"
    r"\.(messages|items|results|rows|records|data|emails|entries|files)\b"
)
_PARSE_ENV = Environment()  # parse-only; never renders

# 007.012: decomposition / decision-vs-work lints. Markers are intentionally
# NARROW (explicit phrases, surrounding spaces) to keep false positives low —
# these gate authoring, so over-firing is worse than the occasional miss.
_QUANTIFIER_MARKERS = (
    " each ", " every ", "for each", "for every", "each of", "one by one",
    "iterate over", "loop over", "go through", "all of the", "all of them",
    "all the ", "list of all", "summarize all", "find all", "every single",
    "all my ", "all your ", "all emails", "all messages", "all rows",
    "all items", "all records", "all files", "all pages", "all the ",
)
_SEQUENCE_MARKERS = (
    " and then ", "and then,", " first, ", "firstly", "secondly", "thirdly",
    "finally,", "step 1", "step one", " then, ",
)
# verb STEMS, matched at a word boundary so inflections count (search/searches/
# searching). Split into I/O work (belongs in a `tool_call`) and processing. The
# UNION drives multi-operation detection (a single step naming several distinct
# verbs is several steps); the I/O set drives the agent-does-work nudge.
_IO_VERB_STEMS = (
    "search", "fetch", "download", "crawl", "scrap", "send", "sent",
    "upload", "notif", "navigat", "brows", "scan", "retriev", "publish",
)
_PROCESS_VERB_STEMS = (
    "summar", "classif", "categor", "extract", "rank", "compil", "aggregat",
    "total", "analy", "generat", "format", "translat", "compar", "draft",
    "produc", "score", "tally", "tabulat", "filter", "dedup",
)
# cues that a step is a genuine JUDGMENT (so an agent_step is justified)
_JUDGMENT_CUES = (
    "decide", "choose", "should ", "evaluate", "is it ", "are these",
    "classif", "rank", " pick ", "which ", "judge", "assess", "determine",
    "compare", "prioriti", "is this", "does this", "worth", "best ",
)


_JINJA_BLOCK = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)


def _verb_hits(p: str, stems: tuple[str, ...]) -> list[str]:
    return sorted({s for s in stems if re.search(r"\b" + re.escape(s), p)})


def _prompt_markers(prompt: str) -> dict[str, Any]:
    """Cheap lexical scan of a prompt's PROSE for decomposition signals. Jinja
    blocks are stripped first so step ids / field names inside `{{ ... }}`
    (e.g. a step called `scan`, or `.collected`) don't masquerade as verbs."""
    raw = _JINJA_BLOCK.sub(" ", (prompt or "").lower().replace("\n", " "))
    p = " " + raw + " "
    io = _verb_hits(p, _IO_VERB_STEMS)
    proc = _verb_hits(p, _PROCESS_VERB_STEMS)
    return {
        "quantifier": any(m in p for m in _QUANTIFIER_MARKERS),
        "sequence": any(m in p for m in _SEQUENCE_MARKERS),
        "io_verbs": io,
        "action_verbs": sorted(set(io) | set(proc)),
        "judgment": any(c in p for c in _JUDGMENT_CUES),
    }


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    message: str
    step_id: str | None = None
    field: str | None = None
    hint: str | None = None
    # 007.012: stable lint id (e.g. "monolithic-playbook") so tests + hints can
    # reference an issue without matching its prose. Additive — omitted from
    # JSON when None, so existing output shapes are unchanged.
    code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


def format_issues(issues: list[ValidationIssue]) -> str:
    if not issues:
        return "OK — no issues."
    lines = []
    for i in issues:
        loc = f"[{i.step_id}.{i.field}]" if i.step_id and i.field else (
            f"[{i.step_id}]" if i.step_id else ""
        )
        line = f"{i.severity.upper()}: {loc} {i.message}".strip()
        if i.hint:
            line += f" — {i.hint}"
        lines.append(line)
    return "\n".join(lines)


# --- public API ---

def validate_definition(
    defn: dict | PlaybookDef,
    *,
    tool_registry: Any = None,
    all_playbooks: dict[str, list[StepDef]] | None = None,
    check_unknown_keys: bool = False,
) -> list[ValidationIssue]:
    """Return all validation issues for a playbook definition. Never raises on a
    bad playbook (reports instead). Raises only on a non-mapping input.

    `check_unknown_keys` only makes sense for the agent's HAND-WRITTEN YAML
    (yaml.safe_load output) — pydantic silently drops unknown keys there, so a
    typo like `iteratot:` would vanish. Never enable it for a stored/round-
    tripped definition (model_dump emits defaulted keys like `fan_in`/
    `max_iterations` on every step, which would all look "unknown").
    """
    issues: list[ValidationIssue] = []

    if isinstance(defn, PlaybookDef):
        pb: PlaybookDef | None = defn
        raw_dict: dict | None = None
    else:
        if not isinstance(defn, dict):
            raise ValueError("Playbook definition must be a mapping")
        raw_dict = defn
        try:
            pb = PlaybookDef.model_validate(defn)
        except ValidationError as e:
            for err in e.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                issues.append(ValidationIssue(
                    severity="error",
                    field=loc or None,
                    message=f"schema: {err.get('msg', 'invalid')}",
                ))
            return issues  # structure is broken; deeper analysis impossible
        except Exception as e:  # noqa: BLE001
            issues.append(ValidationIssue(severity="error", message=f"schema: {e}"))
            return issues

    # 1. required-field checks (safe on raw or round-tripped dicts)
    raw_steps = (
        raw_dict.get("steps", []) if raw_dict is not None
        else pb.model_dump(mode="json", exclude_none=True, by_alias=True).get("steps", [])
    )
    _check_required_steps(raw_steps, issues)

    # 2. unknown-key typo detection — ONLY on agent-authored YAML
    if check_unknown_keys and raw_dict is not None:
        _check_unknown_keys_steps(raw_steps, issues)

    # 3. trigger checks (cron is also blocked by pydantic on the PlaybookDef path)
    if raw_dict is not None:
        _check_triggers(raw_dict.get("triggers", []), issues)

    # 4. reference / scope / tool / context-economy analysis on the parsed tree
    inputs_props = (pb.inputs or {}).get("properties", {}) if isinstance(pb.inputs, dict) else {}
    all_ids = _collect_ids(pb.steps)
    id_to_step = _map_ids(pb.steps)          # 007.009.01: for bad-ref-shape
    vars_set = _collect_state_vars(pb.steps)  # 007.009.01: vars any state op sets
    available: set[str] = set()
    _walk(
        pb.steps,
        available=available,
        item_vars=set(),
        in_loop=False,
        all_ids=all_ids,
        inputs_props=inputs_props,
        tool_registry=tool_registry,
        issues=issues,
        id_to_step=id_to_step,
        vars_set=vars_set,
    )

    # 007.012: monolithic-playbook — the whole task delegated to one step.
    _monolithic_lint(pb, inputs_props, issues)

    # 4. subtask cycles (needs the whole graph)
    if all_playbooks is not None:
        graph = dict(all_playbooks)
        graph[pb.name] = pb.steps
        cycle = detect_subtask_cycles(pb.name, pb.steps, graph)
        if cycle:
            issues.append(ValidationIssue(
                severity="error",
                message=f"subtask cycle: {' → '.join(cycle)}",
                hint="a playbook cannot (transitively) call itself",
            ))

    return issues


# --- raw key / required-field checks ---

def _each_child_list(step: dict):
    for key in ("body", "then", "else", "else_"):
        children = step.get(key)
        if isinstance(children, list):
            yield children
    branches = step.get("branches")
    if isinstance(branches, list):
        for br in branches:
            if isinstance(br, list):
                yield br


def _check_required_steps(steps: list[Any], issues: list[ValidationIssue]) -> None:
    for step in steps:
        if not isinstance(step, dict):
            continue
        _req(step, step.get("kind", ""), step.get("id"), issues)
        for children in _each_child_list(step):
            _check_required_steps(children, issues)


def _check_unknown_keys_steps(steps: list[Any], issues: list[ValidationIssue]) -> None:
    for step in steps:
        if not isinstance(step, dict):
            continue
        kind = step.get("kind", "")
        sid = step.get("id")
        if kind in KIND_KEYS:
            allowed = COMMON_STEP_KEYS | KIND_KEYS[kind]
            unknown = sorted(k for k in step if k not in allowed)
            if unknown:
                issues.append(ValidationIssue(
                    "error", f"unknown key(s) for {kind}: {', '.join(unknown)}",
                    step_id=sid,
                    hint=f"valid: {', '.join(sorted(KIND_KEYS[kind]))}",
                ))
        elif kind:
            issues.append(ValidationIssue(
                "error", f"unknown step kind '{kind}'", step_id=sid,
                hint=f"valid kinds: {', '.join(sorted(KIND_KEYS))}",
            ))
        for children in _each_child_list(step):
            _check_unknown_keys_steps(children, issues)


def _req(step: dict, kind: str, sid: Any, issues: list[ValidationIssue]) -> None:
    def err(msg: str, field: str) -> None:
        issues.append(ValidationIssue("error", msg, step_id=sid, field=field))

    if kind == "tool_call" and not step.get("tool"):
        err("tool_call requires 'tool'", "tool")
    if kind in ("agent_step", "llm_step") and not step.get("prompt"):
        err(f"{kind} requires 'prompt'", "prompt")
    if kind == "condition" and not step.get("when"):
        err("condition requires 'when'", "when")
    if kind == "subtask" and not step.get("playbook"):
        err("subtask requires 'playbook'", "playbook")
    if kind == "wait_for_event" and not step.get("event"):
        err("wait_for_event requires 'event'", "event")
    if kind == "state" and not step.get("state"):
        err("state requires at least one op in 'state'", "state")
    if kind == "loop":
        has_count = "count" in step
        if (
            not step.get("over") and not step.get("until")
            and not step.get("while") and not has_count
        ):
            err("loop requires 'over', 'while', 'until', or 'count'", "over")
        if not step.get("body"):
            err("loop has an empty body — nest steps inside 'body'", "body")


def _check_triggers(triggers: list[Any], issues: list[ValidationIssue]) -> None:
    for t in triggers:
        if not isinstance(t, dict):
            continue
        if t.get("cron"):
            issues.append(ValidationIssue(
                "error", "cron triggers are not supported yet (Phase 014)", field="cron",
            ))
        elif not t.get("event"):
            issues.append(ValidationIssue("error", "trigger must have 'event'", field="event"))


# --- reference / scope analysis ---

def _collect_ids(steps: list[StepDef]) -> set[str]:
    out: set[str] = set()
    for s in steps:
        out.add(s.id)
        for child in (s.then, s.else_, s.body):
            if child:
                out |= _collect_ids(child)
        if s.branches:
            for br in s.branches:
                out |= _collect_ids(br)
    return out


def _map_ids(steps: list[StepDef]) -> dict[str, StepDef]:
    """step_id → StepDef across the whole tree (007.009.01, bad-ref-shape)."""
    out: dict[str, StepDef] = {}
    for s in steps:
        out[s.id] = s
        for child in (s.then, s.else_, s.body):
            if child:
                out.update(_map_ids(child))
        if s.branches:
            for br in s.branches:
                out.update(_map_ids(br))
    return out


def _collect_state_vars(steps: list[StepDef]) -> set[str]:
    """Every var name any `state` op writes, anywhere in the tree."""
    out: set[str] = set()
    for s in steps:
        if s.kind == StepKind.STATE and s.state:
            for op in s.state:
                out.add(op.var)
                if op.into:
                    out.add(op.into)
        for child in (s.then, s.else_, s.body):
            if child:
                out |= _collect_state_vars(child)
        if s.branches:
            for br in s.branches:
                out |= _collect_state_vars(br)
    return out


def _output_keys_for(step: StepDef) -> set[str] | None:
    """Allowed `steps.<id>.<field>` keys for this step's kind, or None when the
    shape is unknowable statically (so we don't flag it)."""
    kind = step.kind.value
    if kind in ("agent_step", "llm_step"):
        schema = step.output_schema
        if isinstance(schema, dict) and schema:
            props = schema.get("properties")
            if isinstance(props, dict) and props:
                return set(props) | {"_dry", "_raw"}
            keys = {k for k in schema if k not in ("type", "required", "properties")}
            if keys:
                return keys | {"_dry", "_raw"}
        # schemaless → the runner wraps raw text as {_raw: ...}
        return {"_raw"}
    return KIND_OUTPUT_KEYS.get(kind)


def _walk(
    steps: list[StepDef],
    *,
    available: set[str],
    item_vars: set[str],
    in_loop: bool,
    all_ids: set[str],
    inputs_props: dict,
    tool_registry: Any,
    issues: list[ValidationIssue],
    id_to_step: dict[str, StepDef],
    vars_set: set[str],
) -> None:
    _hardcoded_fanout_lint(steps, issues)  # 007.009.01 (sibling-level)
    for step in steps:
        kw = dict(
            all_ids=all_ids, inputs_props=inputs_props, issues=issues,
            id_to_step=id_to_step, vars_set=vars_set,
        )
        if step.kind == StepKind.LOOP:
            if isinstance(step.over, str):
                _check_source(
                    step.over, is_expr=True, item_vars=item_vars, available=available,
                    step_id=step.id, field="over", **kw,
                )
            inner_items = set(item_vars)
            if step.item_name:
                inner_items.add(step.item_name)
                inner_items.add(f"{step.item_name}_index")
            body_ids = _collect_ids(step.body or [])
            loop_expr_avail = available | {step.id} | body_ids
            for fld, src in (("until", step.until), ("while", step.while_),
                             ("break_when", step.break_when), ("collect", step.collect)):
                if src:
                    _check_source(
                        src, is_expr=True, item_vars=inner_items, available=loop_expr_avail,
                        step_id=step.id, field=fld, **kw,
                    )
            _loop_progress_lint(step, issues)  # 007.009.01
            available.add(step.id)
            _walk(
                step.body or [], available=available, item_vars=inner_items, in_loop=True,
                all_ids=all_ids, inputs_props=inputs_props, tool_registry=tool_registry,
                issues=issues, id_to_step=id_to_step, vars_set=vars_set,
            )
            # 007.009.01: a concurrent map must not mutate shared state.
            if (step.concurrency or 1) > 1:
                _concurrent_state_lint(step, issues)
        elif step.kind == StepKind.CONDITION:
            if step.when:
                _check_source(
                    step.when, is_expr=True, item_vars=item_vars, available=available,
                    step_id=step.id, field="when", **kw,
                )
            available.add(step.id)
            for branch in (step.then or [], step.else_ or []):
                _walk(
                    branch, available=available, item_vars=item_vars, in_loop=in_loop,
                    all_ids=all_ids, inputs_props=inputs_props, tool_registry=tool_registry,
                    issues=issues, id_to_step=id_to_step, vars_set=vars_set,
                )
        elif step.kind == StepKind.PARALLEL:
            available.add(step.id)
            for br in (step.branches or []):
                _walk(
                    br, available=available, item_vars=item_vars, in_loop=in_loop,
                    all_ids=all_ids, inputs_props=inputs_props, tool_registry=tool_registry,
                    issues=issues, id_to_step=id_to_step, vars_set=vars_set,
                )
        else:
            _check_leaf(
                step, available=available, item_vars=item_vars, in_loop=in_loop,
                all_ids=all_ids, inputs_props=inputs_props, tool_registry=tool_registry,
                issues=issues, id_to_step=id_to_step, vars_set=vars_set,
            )
            available.add(step.id)


def _check_leaf(
    step: StepDef,
    *,
    available: set[str],
    item_vars: set[str],
    in_loop: bool,
    all_ids: set[str],
    inputs_props: dict,
    tool_registry: Any,
    issues: list[ValidationIssue],
    id_to_step: dict[str, StepDef],
    vars_set: set[str],
) -> None:
    kw = dict(
        item_vars=item_vars, available=available, all_ids=all_ids,
        inputs_props=inputs_props, issues=issues, id_to_step=id_to_step,
        vars_set=vars_set,
    )
    if step.kind == StepKind.TOOL_CALL:
        if step.tool and tool_registry is not None:
            try:
                tool_registry.get(step.tool)
            except KeyError:
                issues.append(ValidationIssue(
                    "error", f"unknown tool '{step.tool}' — not in the tool registry",
                    step_id=step.id, field="tool",
                    hint="use a tool from your tool list, or an agent_step instead",
                ))
        for key, val in (step.args or {}).items():
            _check_value(val, step_id=step.id, field=f"args.{key}", **kw)
    if step.prompt:
        _check_source(step.prompt, is_expr=False, step_id=step.id, field="prompt", **kw)
        if step.kind in (StepKind.AGENT_STEP, StepKind.LLM_STEP):
            if not in_loop:
                _context_economy_lint(step, inputs_props, issues)
                _compound_leaf_lint(step, issues)  # 007.012: hidden loop / multi-step
            _agent_does_work_lint(step, issues)  # 007.012: work belongs in tool_call
    if step.system:
        _check_source(step.system, is_expr=False, step_id=step.id, field="system", **kw)
    if step.kind == StepKind.SUBTASK:
        for key, val in (step.inputs_map or {}).items():
            _check_value(val, step_id=step.id, field=f"inputs_map.{key}", **kw)
    if step.kind == StepKind.STATE:
        # 007.009.01: each op `value` is a Jinja expression; `into`/`var` are
        # plain var names (not refs).
        for i, op in enumerate(step.state or []):
            if isinstance(op.value, str):
                _check_source(
                    op.value, is_expr=True, step_id=step.id,
                    field=f"state[{i}].value", **kw,
                )
            if op.op in ("pop_back", "pop_front") and not op.into:
                issues.append(ValidationIssue(
                    "warning", f"{op.op} on 'vars.{op.var}' has no 'into' — the "
                    "popped value is discarded", step_id=step.id, field=f"state[{i}]",
                    hint="set into: <var_name> to capture what you popped",
                ))
    if step.kind == StepKind.HALT:
        if step.when:
            _check_source(step.when, is_expr=True, step_id=step.id, field="when", **kw)
        if isinstance(step.value, str):
            _check_source(step.value, is_expr=True, step_id=step.id, field="value", **kw)


def _check_value(val: Any, **kw: Any) -> None:
    """Recurse into a templated arg value (dict/list/str)."""
    if isinstance(val, str):
        _check_source(val, is_expr=False, **kw)
    elif isinstance(val, dict):
        for v in val.values():
            _check_value(v, **kw)
    elif isinstance(val, list):
        for v in val:
            _check_value(v, **kw)


def _check_source(
    source: str,
    *,
    is_expr: bool,
    item_vars: set[str],
    available: set[str],
    all_ids: set[str],
    inputs_props: dict,
    step_id: str,
    field: str,
    issues: list[ValidationIssue],
    id_to_step: dict[str, StepDef] | None = None,
    vars_set: set[str] | None = None,
) -> None:
    if not isinstance(source, str) or not source:
        return
    expr = source
    if is_expr:
        expr = expr.strip()
        while expr.startswith("{{") and expr.endswith("}}"):
            expr = expr[2:-2].strip()
    has_jinja = "{{" in source or "{%" in source
    if not is_expr and not has_jinja:
        return  # plain string, nothing to analyze

    wrapped = "{{ (" + expr + ") }}" if is_expr else source
    try:
        ast = _PARSE_ENV.parse(wrapped)
    except TemplateSyntaxError as e:
        issues.append(ValidationIssue(
            "error", f"template syntax error: {e.message}", step_id=step_id, field=field,
        ))
        return

    allowed = {"inputs", "steps", "vars"} | _BUILTINS | item_vars
    for root in sorted(meta.find_undeclared_variables(ast)):
        if root not in allowed:
            avail = ", ".join(
                ["inputs.*", "vars.*", "steps.<id>.*"] + sorted(item_vars)
            )
            issues.append(ValidationIssue(
                "error", f"unknown variable '{root}'", step_id=step_id, field=field,
                hint=f"available: {avail}",
            ))

    for m in _STEP_REF.finditer(source):
        sid = m.group(1)
        attr = m.group(2)
        if sid not in all_ids:
            issues.append(ValidationIssue(
                "error", f"references steps.{sid} — no such step", step_id=step_id, field=field,
            ))
            continue
        if sid not in available and sid != step_id:
            issues.append(ValidationIssue(
                "error", f"steps.{sid} used before it runs", step_id=step_id, field=field,
                hint="reference only steps that execute earlier",
            ))
        # 007.009.01: bad-ref-shape — `.field` must be a real output key for
        # that step's kind. Catches `.output` on a schemaless llm_step.
        elif attr and id_to_step is not None:
            target = id_to_step.get(sid)
            if target is not None:
                allowed_keys = _output_keys_for(target)
                if allowed_keys is not None and attr not in allowed_keys:
                    hint = f"valid: {', '.join(sorted(allowed_keys))}"
                    if attr == "output":
                        hint = (
                            f"a schemaless llm_step/agent_step returns {{_raw: ...}} — "
                            f"use steps.{sid}._raw, or declare an output_schema"
                        )
                    issues.append(ValidationIssue(
                        "error",
                        f"steps.{sid}.{attr} — '{attr}' is not an output of "
                        f"{target.kind.value} step '{sid}'",
                        step_id=step_id, field=field, hint=hint,
                    ))

    # 007.009.01: vars.<x> referenced but never written by any state op.
    if vars_set is not None:
        for m in re.finditer(r"\bvars\.([A-Za-z_][A-Za-z0-9_]*)", source):
            vk = m.group(1)
            if vk not in vars_set:
                issues.append(ValidationIssue(
                    "warning", f"vars.{vk} is never set by any state step",
                    step_id=step_id, field=field,
                    hint="add a state step (op: set) before reading it",
                ))

    if inputs_props:
        for m in _INPUT_REF.finditer(source):
            k = m.group(1)
            if k not in inputs_props:
                issues.append(ValidationIssue(
                    "warning", f"inputs.{k} is not declared in the inputs schema",
                    step_id=step_id, field=field,
                ))


def _context_economy_lint(
    step: StepDef, inputs_props: dict, issues: list[ValidationIssue],
) -> None:
    """007.009 Goal 6: warn when a single (non-looped) LLM/agent step dumps a
    whole collection into the model — iterate + collect instead."""
    prompt = step.prompt or ""
    flagged: str | None = None
    deep = _DEEP_COLLECTION_REF.search(prompt)
    if deep:
        flagged = deep.group(0)
    if not flagged:
        for m in _INPUT_REF.finditer(prompt):
            k = m.group(1)
            prop = inputs_props.get(k) if isinstance(inputs_props, dict) else None
            if isinstance(prop, dict) and prop.get("type") == "array":
                flagged = f"inputs.{k}"
                break
    if flagged:
        issues.append(ValidationIssue(
            "warning",
            f"this {step.kind.value} feeds a whole collection ({flagged}) into one "
            "model call — that can explode the context window",
            step_id=step.id, field="prompt",
            hint="loop over the items, summarize ONE per iteration, then collect",
        ))


# 007.012: kinds that DO actual work (vs. structural/plumbing kinds). Used by
# the monolithic-playbook lint to count "is this whole playbook one step?".
_WORK_KINDS = {
    StepKind.AGENT_STEP, StepKind.LLM_STEP, StepKind.TOOL_CALL, StepKind.SUBTASK,
}


def _iter_all_steps(steps: list[StepDef]):
    """Every step anywhere in the tree (incl. loop bodies, branches)."""
    for s in steps:
        yield s
        for child in (s.then, s.else_, s.body):
            if child:
                yield from _iter_all_steps(child)
        if s.branches:
            for br in s.branches:
                yield from _iter_all_steps(br)


def _is_delivery(step: StepDef) -> bool:
    """A pure 'show the owner' tool_call — not real work for the monolith count."""
    return step.kind == StepKind.TOOL_CALL and step.tool == "send_chat_message"


def _reads_collection(prompt: str, inputs_props: dict) -> bool:
    """Does this prompt feed a whole collection into the model? (array input or a
    deep `.messages/.results/...` ref.)"""
    if _DEEP_COLLECTION_REF.search(prompt or ""):
        return True
    for m in _INPUT_REF.finditer(prompt or ""):
        prop = inputs_props.get(m.group(1)) if isinstance(inputs_props, dict) else None
        if isinstance(prop, dict) and prop.get("type") == "array":
            return True
    return False


def _compound_leaf_lint(step: StepDef, issues: list[ValidationIssue]) -> None:
    """007.012: a single (non-looped) llm/agent step whose prompt hides a loop or
    several operations. Complements context-economy: that fires on the REFERENCE
    shape, this one on the LANGUAGE."""
    m = _prompt_markers(step.prompt or "")
    reasons: list[str] = []
    if m["quantifier"]:
        reasons.append("a quantifier (each/every/all)")
    if m["sequence"]:
        reasons.append("a sequence (and-then / numbered steps)")
    if len(m["action_verbs"]) >= 2:
        reasons.append(f"multiple operations ({', '.join(m['action_verbs'])})")
    if reasons:
        issues.append(ValidationIssue(
            "warning",
            f"this {step.kind.value} prompt describes " + "; ".join(reasons)
            + " — that is a hidden loop / multi-step",
            step_id=step.id, field="prompt", code="compound-leaf",
            hint="loop over the items and do ONE operation per iteration "
                 "(collect the results), or split into separate steps",
        ))


def _agent_does_work_lint(step: StepDef, issues: list[ValidationIssue]) -> None:
    """007.012: an agent_step doing mechanical work (fetch/search/send) with no
    judgment cue — that work belongs in a tool_call. Reserve agents for
    decisions. Conservative: needs a work verb AND zero judgment cue."""
    if step.kind != StepKind.AGENT_STEP:
        return
    m = _prompt_markers(step.prompt or "")
    if m["io_verbs"] and not m["judgment"]:
        issues.append(ValidationIssue(
            "warning",
            f"this agent_step looks like mechanical work "
            f"({', '.join(m['io_verbs'])}) with no judgment",
            step_id=step.id, field="prompt", code="agent-does-work",
            hint="reserve agent_step for DECISIONS; do work with a tool_call (or a "
                 "loop of tool_calls). Call an agent only for judgment the graph "
                 "can't express deterministically",
        ))


def _monolithic_lint(
    pb: PlaybookDef, inputs_props: dict, issues: list[ValidationIssue],
) -> None:
    """007.012: the whole playbook is ONE delegated work step that hides a
    process — a 'prompt wearing a playbook costume'. Error (blocks authoring).
    A genuine single judgment ('draft a reply to THIS email') has no quantifier
    and no collection, so it passes untouched."""
    work = [
        s for s in _iter_all_steps(pb.steps)
        if s.kind in _WORK_KINDS and not _is_delivery(s)
    ]
    if len(work) != 1:
        return
    s = work[0]
    if s.kind not in (StepKind.AGENT_STEP, StepKind.LLM_STEP):
        return  # a single tool_call/subtask is a fine atomic action
    m = _prompt_markers(s.prompt or "")
    # multiple operations that include I/O = a fetch+process+deliver pipeline
    # crammed into one prompt (the email-digest monolith the dojo caught).
    multi_op_io = len(m["action_verbs"]) >= 2 and bool(m["io_verbs"])
    if (m["quantifier"] or m["sequence"] or multi_op_io
            or _reads_collection(s.prompt or "", inputs_props)):
        issues.append(ValidationIssue(
            "error",
            f"this playbook delegates the whole task to a single step "
            f"('{s.id}') — that is a prompt wearing a playbook costume",
            step_id=s.id, field="prompt", code="monolithic-playbook",
            hint="decompose: fetch (tool_call) -> loop(ONE judgment per item, "
                 "collect) -> reduce (llm_step) -> deliver (send_chat_message)",
        ))


def _hardcoded_fanout_lint(
    steps: list[StepDef], issues: list[ValidationIssue],
) -> None:
    """007.009.01: warn when ≥3 sibling tool_calls hit the SAME tool with only
    literal (no `{{ }}`) args — the "I hardcoded the pages I guessed" anti-
    pattern (monday-sitemap-builder). Discover at run time instead."""
    by_tool: dict[str, list[StepDef]] = {}
    for s in steps:
        if s.kind == StepKind.TOOL_CALL and s.tool:
            args_literal = not _has_template(s.args or {})
            if args_literal:
                by_tool.setdefault(s.tool, []).append(s)
    for tool, group in by_tool.items():
        if len(group) >= 3:
            ids = ", ".join(s.id for s in group)
            issues.append(ValidationIssue(
                "warning",
                f"{len(group)} sibling tool_calls to '{tool}' with hardcoded args "
                f"({ids}) — looks like hand-listed items",
                step_id=group[0].id,
                hint="discover at run time: loop/while over a frontier (state), or "
                     "use an agent_step — don't hardcode items you could fetch",
            ))


def _has_template(val: Any) -> bool:
    if isinstance(val, str):
        return "{{" in val or "{%" in val
    if isinstance(val, dict):
        return any(_has_template(v) for v in val.values())
    if isinstance(val, list):
        return any(_has_template(v) for v in val)
    return False


def _loop_progress_lint(step: StepDef, issues: list[ValidationIssue]) -> None:
    """007.009.01: a `while`/`until` loop whose body never mutates state nor any
    step output may not progress (it relies on something changing each pass).
    Warn — it will run to max_iterations."""
    if not (step.while_ or step.until):
        return
    body = step.body or []
    has_state = _body_has_state(body)
    if not has_state:
        issues.append(ValidationIssue(
            "warning",
            f"loop '{step.id}' uses while/until but its body has no state step — "
            "the condition may never change (it will run to max_iterations)",
            step_id=step.id, field="while" if step.while_ else "until",
            hint="mutate a var with a state step each iteration (e.g. pop a queue)",
        ))


def _body_has_state(steps: list[StepDef]) -> bool:
    for s in steps:
        if s.kind == StepKind.STATE:
            return True
        for child in (s.then, s.else_, s.body):
            if child and _body_has_state(child):
                return True
        if s.branches and any(_body_has_state(br) for br in s.branches):
            return True
    return False


def _concurrent_state_lint(step: StepDef, issues: list[ValidationIssue]) -> None:
    """007.009.01: a concurrency>1 map runs item bodies in isolated scopes, so a
    shared-state mutation would be lost/raced. Forbid it (error)."""
    if _body_has_state(step.body or []):
        issues.append(ValidationIssue(
            "error",
            f"loop '{step.id}' has concurrency>1 AND a state step in its body — "
            "state mutations are not shared across a concurrent map",
            step_id=step.id, field="concurrency",
            hint="use concurrency: 1 for a shared frontier/accumulator",
        ))
