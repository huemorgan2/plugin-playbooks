"""Plugin-owned test utilities — stub everything a playbook touches.

Lives inside the plugin (not tests/) so the plugin is self-testable in
isolation and downstream phases can reuse the same fakes. Provides:

- `FakeAgent`     — run_turn / run_llm without a real model (records calls).
- `make_fake_tool` / `build_tool_registry` — deterministic tool_call steps.
- `FakeTriggerSource` — a luna.triggers source for binding tests (records
  ensure/release transitions).
- `build_test_runner` — in-memory SQLite + EventBus + fakes → a ready
  PlaybookRunner.
- `make_playbook` — insert a Playbook row from a definition dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from luna_sdk import EventBus, ToolDef, ToolRegistry, TriggerInfo


@dataclass
class _FakeUsage:
    cost_cents: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


def _shape_from_schema(schema: Any) -> dict[str, Any]:
    """Type-correct placeholder dict from an output_schema (JSON-schema or
    shorthand)."""
    by_type = {
        "string": "", "str": "", "number": 0, "integer": 0, "int": 0,
        "boolean": False, "bool": False, "object": {}, "array": [], "list": [],
    }

    def one(spec: Any) -> Any:
        if isinstance(spec, dict):
            return by_type.get(str(spec.get("type", "")).lower(), "")
        return by_type.get(str(spec).lower(), "")

    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            return {k: one(v) for k, v in props.items()}
        shorthand = {
            k: one(v) for k, v in schema.items()
            if k not in ("type", "required", "properties")
        }
        if shorthand:
            return shorthand
    return {"_fake": True}


class FakeAgent:
    """Stands in for LunaAgent for agent_step / llm_step without a real model.

    Pass `turn_response` / `llm_response` to pin outputs, or rely on the
    output_schema-shaped default. All calls are recorded for assertions.
    """

    def __init__(
        self,
        *,
        turn_response: Any = None,
        llm_response: Any = None,
        cost_cents: float = 0.0,
    ) -> None:
        self._turn_response = turn_response
        self._llm_response = llm_response
        # Non-zero cost exercises the runner's per-step cost-write path. Default
        # 0.0 keeps that path inert (it short-circuits on falsy cost), which is
        # exactly why a loop+llm_step cost regression hid from coded tests until
        # the dojo run surfaced it.
        self._cost_cents = cost_cents
        self.turn_calls: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []

    async def run_turn(
        self,
        prompt: str,
        *,
        output_schema: dict[str, Any] | None = None,
        tools: list[str] | None = None,
        identity: dict[str, Any] | None = None,
        memory_write: bool = False,
        memory_read: bool = True,
        conversation_id: Any = None,
    ) -> tuple[Any, _FakeUsage]:
        self.turn_calls.append({
            "prompt": prompt, "output_schema": output_schema, "tools": tools,
            "conversation_id": conversation_id,
        })
        usage = _FakeUsage(cost_cents=self._cost_cents)
        if self._turn_response is not None:
            return self._turn_response, usage
        if output_schema:
            return _shape_from_schema(output_schema), usage
        return f"[fake agent_step] {prompt[:80]}", usage

    async def run_llm(
        self,
        prompt: str,
        *,
        purpose: str = "summarization",
        model: str | None = None,
        system: str | None = None,
        output_schema: dict[str, Any] | None = None,
        temperature: float = 1.0,
        max_tokens: int | None = None,
    ) -> tuple[Any, _FakeUsage]:
        self.llm_calls.append({
            "prompt": prompt, "purpose": purpose, "model": model,
            "system": system, "output_schema": output_schema,
        })
        usage = _FakeUsage(cost_cents=self._cost_cents)
        if self._llm_response is not None:
            return self._llm_response, usage
        if output_schema:
            return _shape_from_schema(output_schema), usage
        return f"[fake llm_step] {prompt[:80]}", usage


def make_fake_tool(
    name: str,
    result: Any = None,
    *,
    parameters: dict[str, Any] | None = None,
) -> tuple[ToolDef, Any]:
    """Return (ToolDef, handler) for a deterministic tool. `result` may be a
    callable(**kwargs) or a static value."""

    async def _handler(**kwargs: Any) -> Any:
        if callable(result):
            out = result(**kwargs)
            return out
        return result if result is not None else {"ok": True, "args": kwargs}

    return (
        ToolDef(
            name=name,
            description=f"fake tool {name}",
            parameters=parameters or {"type": "object", "properties": {}},
        ),
        _handler,
    )


def build_tool_registry(tools: list[tuple[ToolDef, Any]] | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    for tool_def, handler in (tools or []):
        reg.register("test-playbooks", tool_def, handler)
    return reg


class FakeTriggerSource:
    """A luna.triggers TriggerSource for binding tests. Records ensure/release
    transitions so the consumer (TriggerBindingService) can be asserted."""

    def __init__(self, source_name: str = "fake", triggers: list[TriggerInfo] | None = None):
        self.source_name = source_name
        self._triggers = triggers or []
        self.ensured: list[str] = []
        self.released: list[str] = []

    async def list_triggers(self, app: str | None = None) -> list[TriggerInfo]:
        if app is None:
            return list(self._triggers)
        return [t for t in self._triggers if t.app == app]

    async def ensure_trigger(self, slug: str, config: dict[str, Any]) -> str:
        self.ensured.append(slug)
        return f"instance-{slug}"

    async def release_trigger(self, slug: str) -> None:
        self.released.append(slug)


@dataclass
class TestRunnerBundle:
    runner: Any
    session_factory: async_sessionmaker
    events: EventBus
    agent: FakeAgent
    tool_registry: ToolRegistry
    engine: Any


async def build_test_runner(
    *,
    tools: list[tuple[ToolDef, Any]] | None = None,
    agent: FakeAgent | None = None,
    events: EventBus | None = None,
) -> TestRunnerBundle:
    """Wire an in-memory SQLite DB + EventBus + FakeAgent + fake tools into a
    ready PlaybookRunner. Caller disposes via `bundle.engine.dispose()`."""
    from .models import (
        Playbook, PlaybookDraft, PlaybookRun, PlaybookStepRun, PlaybookVersion,
    )
    from .runner import PlaybookRunner

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for table in (
            Playbook.__table__, PlaybookVersion.__table__,
            PlaybookRun.__table__, PlaybookStepRun.__table__,
            PlaybookDraft.__table__,
        ):
            await conn.run_sync(table.create, checkfirst=True)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    fake_agent = agent or FakeAgent()
    bus = events or EventBus()
    registry = build_tool_registry(tools)
    runner = PlaybookRunner(
        session_factory=sf, tool_registry=registry, events=bus, agent=fake_agent,
    )
    return TestRunnerBundle(
        runner=runner, session_factory=sf, events=bus, agent=fake_agent,
        tool_registry=registry, engine=engine,
    )


async def make_playbook(
    session_factory: async_sessionmaker, definition: dict[str, Any], *, status: str = "enabled",
) -> Any:
    from .models import Playbook

    async with session_factory() as s:
        pb = Playbook(name=definition["name"], definition=definition, status=status)
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
        return pb
