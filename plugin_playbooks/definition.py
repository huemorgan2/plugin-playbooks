"""PlaybookDef / StepDef — pydantic models for playbook definitions.

These are the user-facing data structures for defining playbooks.
They validate the YAML/JSON and compile to the DB JSON column.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class StepKind(str, Enum):
    TOOL_CALL = "tool_call"
    AGENT_STEP = "agent_step"
    LLM_STEP = "llm_step"
    CONDITION = "condition"
    PARALLEL = "parallel"
    WAIT_FOR_APPROVAL = "wait_for_approval"
    WAIT_FOR_EVENT = "wait_for_event"
    SUBTASK = "subtask"
    LOOP = "loop"
    # 007.009.01: run-scoped state + early return.
    STATE = "state"
    HALT = "halt"


# 007.009.01: ops a `state` step can apply to a run-scoped variable. One `state`
# step may carry several ops (applied in order) so e.g. a crawl can dequeue the
# current URL and mark it visited atomically. Stack vs queue is just which end
# you pop: push_back + pop_back = stack (LIFO); push_back + pop_front = queue
# (FIFO).
StateOpName = Literal[
    "set", "append", "extend",
    "push_back", "push_front", "pop_back", "pop_front",
    "add_unique", "incr", "decr", "merge", "delete",
]


class StateOp(BaseModel):
    op: StateOpName
    var: str
    # literal value OR a Jinja expression ("{{ ... }}") evaluated at run time.
    value: Any | None = None
    # for pop_back / pop_front: the var to store the popped element into.
    into: str | None = None

    model_config = {"populate_by_name": True}


class AgentAutonomy(str, Enum):
    MANUAL_ONLY = "manual_only"
    AGENT_MAY_TRIGGER = "agent_may_trigger"
    AGENT_MUST_CONFIRM = "agent_must_confirm"


class OnError(str, Enum):
    ABORT = "abort"
    CONTINUE = "continue"
    ESCALATE = "escalate"


class RetryConfig(BaseModel):
    max: int = 0
    backoff_seconds: float = 1.0


class TriggerDef(BaseModel):
    event: str | None = None
    cron: str | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    map: dict[str, str] = Field(default_factory=dict)
    if_expr: str | None = Field(default=None, alias="if")

    @model_validator(mode="after")
    def _validate_trigger_type(self):
        if not self.event and not self.cron:
            raise ValueError("Trigger must have 'event' or 'cron'")
        if self.cron:
            raise ValueError("Cron triggers are not supported yet (Phase 014)")
        return self

    model_config = {"populate_by_name": True}


class StepDef(BaseModel):
    """One step in a playbook definition."""

    id: str
    kind: StepKind

    # tool_call
    tool: str | None = None
    args: dict[str, Any] | None = None

    # agent_step (prompt + output_schema shared with llm_step)
    prompt: str | None = None
    output_schema: dict[str, Any] | None = None
    tools: list[str] | None = None

    # llm_step (007.006): a raw model call, no agent scaffolding.
    # `purpose` picks the router chain (default summarization → Haiku);
    # `model` ("provider/model") force-pins one; `system` is optional.
    purpose: str | None = None
    model: str | None = None
    system: str | None = None

    # condition
    when: str | None = None
    then: list[StepDef] | None = None
    else_: list[StepDef] | None = Field(default=None, alias="else")

    # parallel
    branches: list[list[StepDef]] | None = None
    fan_in: str = "all"

    # wait_for_approval
    show: list[str] | None = None

    # wait_for_event
    event: str | None = None
    event_filter: dict[str, Any] | None = None
    timeout_seconds: int | None = None

    # subtask
    playbook: str | None = None
    inputs_map: dict[str, str] | None = None
    # 007.009.01: expose sub-workflow outputs to the parent. Each value is a
    # Jinja expression evaluated against the sub-run's step outputs; the result
    # is merged so the parent can read steps.<subtask_id>.<key>.
    returns: dict[str, str] | None = None

    # loop
    # 006.712: `over` accepts a literal list (used as-is) or a string
    # expression (evaluated at run time). `item_name` exposes the current
    # item as a top-level template var inside the body ({{ number }}).
    over: str | list[Any] | None = None
    body: list[StepDef] | None = None
    max_iterations: int = 100
    until: str | None = None
    # 007.009.01: `while` loops *while* the expression is truthy (complement of
    # `until`). With run-scoped `vars` persisting across iterations this makes a
    # growing frontier (BFS/DFS crawl) expressible.
    while_: str | None = Field(default=None, alias="while")
    # 007.009.01: stop the loop early after an iteration when truthy.
    break_when: str | None = None
    # 007.009.01: bounded-concurrency map over `over` items (1 = sequential).
    concurrency: int = 1
    item_name: str | None = None
    # 007.005: a Jinja expression evaluated after each iteration's body (item
    # vars still in scope). Each result is appended to a list exposed as
    # steps.<loop_id>.collected — the way to gather per-iteration outputs.
    collect: str | None = None

    # state (007.009.01)
    state: list[StateOp] | None = None

    # halt (007.009.01): optional guard + final value (the run's result).
    value: Any | None = None

    # common
    explanation: str = ""
    retry: RetryConfig = Field(default_factory=RetryConfig)
    on_error: OnError = OnError.ABORT
    timeout: int | None = None

    model_config = {"populate_by_name": True}


class PlaybookDef(BaseModel):
    """The full definition of a playbook (what gets stored as JSON in DB)."""

    name: str
    display_name: str = ""
    description: str = ""
    explanation: str = ""
    when_to_use: str = ""
    agent_autonomy: AgentAutonomy = AgentAutonomy.AGENT_MUST_CONFIRM
    inputs: dict[str, Any] = Field(default_factory=lambda: {
        "type": "object", "properties": {},
    })
    triggers: list[TriggerDef] = Field(default_factory=list)
    steps: list[StepDef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_step_ids_unique(self):
        ids = set()
        for step_id in _collect_step_ids(self.steps):
            if step_id in ids:
                raise ValueError(f"Duplicate step id: '{step_id}'")
            ids.add(step_id)
        return self


def _collect_step_ids(steps: list[StepDef]) -> list[str]:
    """Recursively collect all step IDs for uniqueness validation."""
    ids: list[str] = []
    for s in steps:
        ids.append(s.id)
        if s.then:
            ids.extend(_collect_step_ids(s.then))
        if s.else_:
            ids.extend(_collect_step_ids(s.else_))
        if s.body:
            ids.extend(_collect_step_ids(s.body))
        if s.branches:
            for branch in s.branches:
                ids.extend(_collect_step_ids(branch))
    return ids


def _iter_subtask_targets(steps: list[StepDef]):
    """Yield every subtask target playbook referenced anywhere in `steps`,
    including inside loop bodies, condition branches, and parallel branches."""
    for step in steps:
        if step.kind == StepKind.SUBTASK and step.playbook:
            yield step.playbook
        if step.then:
            yield from _iter_subtask_targets(step.then)
        if step.else_:
            yield from _iter_subtask_targets(step.else_)
        if step.body:
            yield from _iter_subtask_targets(step.body)
        if step.branches:
            for branch in step.branches:
                yield from _iter_subtask_targets(branch)


def detect_subtask_cycles(
    playbook_name: str,
    steps: list[StepDef],
    all_playbooks: dict[str, list[StepDef]],
    visited: set[str] | None = None,
) -> list[str] | None:
    """Full transitive graph walk for subtask cycle detection.

    Returns the cycle path if found, None otherwise.

    The `visited` guard tracks PLAYBOOKS reached via subtask edges — it is NOT
    re-applied when descending into a playbook's own control-flow children
    (loop body / then / else / branches). Otherwise any playbook that merely
    contains a loop or condition would falsely report a cycle back to itself.
    """
    if visited is None:
        visited = set()
    if playbook_name in visited:
        return [playbook_name]
    visited = visited | {playbook_name}
    for target in _iter_subtask_targets(steps):
        target_steps = all_playbooks.get(target, [])
        result = detect_subtask_cycles(target, target_steps, all_playbooks, visited)
        if result is not None:
            return [playbook_name] + result
    return None


def parse_yaml(text: str) -> PlaybookDef:
    """Parse a YAML playbook definition string."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Playbook definition must be a YAML mapping")
    return PlaybookDef.model_validate(data)


def to_yaml(playbook: PlaybookDef) -> str:
    """Serialize a PlaybookDef to YAML."""
    data = playbook.model_dump(mode="json", exclude_none=True, by_alias=True)
    return yaml.dump(data, default_flow_style=False, sort_keys=False)
