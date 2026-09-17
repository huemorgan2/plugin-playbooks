"""Legacy-v1 runtime safety regressions.

Four baseline defects from fix-playbooks stage 1 were pinned here. Plan 035
bounded the tool-step timeout. Plan 036 makes the other three fail closed:

1. A restarted in-flight legacy effect has an unknown outcome, not a
   proven failure; it must never be replayed without reconciliation.
2. A legacy approval wait must never auto-approve.
3. A legacy event wait must never claim a missing event was received.
4. step.timeout on tool_call steps was not enforced; plan 035 bounds it
   without blindly replaying an uncertain effect.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _Tool:
    def __init__(self, handler) -> None:
        self.handler = handler


class _Tools:
    def __init__(self, **tools) -> None:
        self._tools = tools

    def get(self, name: str):
        return self._tools[name]


def _playbook(name: str, steps: list[dict]) -> Playbook:
    return Playbook(
        name=name, display_name=name,
        definition={"name": name, "steps": steps}, status="enabled",
    )


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    calls: list[str] = []
    gate = asyncio.Event()

    async def fast_tool(**_kw):
        calls.append("fast")
        return {"ok": True}

    async def slow_tool(**_kw):
        calls.append("slow-started")
        await gate.wait()
        calls.append("slow-finished")
        return {"ok": True}

    tools = _Tools(fast=_Tool(fast_tool), slow=_Tool(slow_tool))
    runner = PlaybookRunner(session_factory=sf, tool_registry=tools, events=_Bus())
    yield sf, tools, runner, calls, gate
    gate.set()  # release parked runs so tasks don't leak
    await asyncio.sleep(0.05)
    await engine.dispose()


async def _save(sf, pb: Playbook) -> Playbook:
    async with sf() as s:
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
    return pb


async def _run_row(sf, run_id) -> PlaybookRun:
    async with sf() as s:
        return await s.get(PlaybookRun, run_id)


async def test_interrupted_legacy_effect_is_unknown_without_replay(env):
    """An in-flight legacy tool has no journal; its outcome needs inspection."""
    sf, tools, runner1, calls, gate = env
    pb = await _save(sf, _playbook("long-job", [
        {"id": "s1", "kind": "tool_call", "tool": "slow", "args": {}},
        {"id": "s2", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    run = await runner1.start_run_background(pb, inputs={})
    while "slow-started" not in calls:  # step 1 is genuinely in flight
        await asyncio.sleep(0.01)

    # "Restart": a fresh runner on the same DB (its _tasks is empty, exactly
    # like plugin load after a process death) runs its on-load sweep.
    runner2 = PlaybookRunner(session_factory=sf, tool_registry=tools, events=_Bus())
    await runner2.sweep_orphaned_runs()

    row = await _run_row(sf, run.id)
    assert row.status == "timed_out_unknown"
    assert row.error_type == "OutcomeUnknown"
    assert "s1" in (row.error or "")
    assert calls == ["slow-started"]  # no replay and no downstream step


async def test_legacy_approval_wait_fails_closed_before_any_effect(env):
    """Old definitions must migrate; a fake approval is never a success."""
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("gated", [
        {"id": "before", "kind": "tool_call", "tool": "fast", "args": {}},
        {"id": "gate", "kind": "wait_for_approval", "show": ["summary"]},
        {"id": "after", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2)

    assert calls == []
    assert row.status == "failed"
    assert row.error_type == "LegacyWaitUnsupported"
    assert "migrate" in (row.error or "").lower()


async def test_legacy_event_wait_fails_closed_before_any_effect(env):
    """Old definitions must migrate; a stub event is never a success."""
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("event-waiter", [
        {"id": "before", "kind": "tool_call", "tool": "fast", "args": {}},
        {"id": "w", "kind": "wait_for_event", "event": "email.received"},
        {"id": "after", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2)

    assert calls == []
    assert row.status == "failed"
    assert row.error_type == "LegacyWaitUnsupported"
    assert "migrate" in (row.error or "").lower()


@pytest.mark.parametrize("nested", [
    {"id": "branch", "kind": "condition", "when": "true", "then": [
        {"id": "gate", "kind": "wait_for_approval"},
    ]},
    {"id": "batch", "kind": "parallel", "branches": [[
        {"id": "event", "kind": "wait_for_event", "event": "order.paid"},
    ]]},
    {"id": "items", "kind": "loop", "over": [], "body": [
        {"id": "event", "kind": "wait_for_event", "event": "order.paid"},
    ]},
])
async def test_nested_legacy_wait_is_rejected_before_any_effect(env, nested):
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("nested-wait", [
        {"id": "before", "kind": "tool_call", "tool": "fast", "args": {}},
        nested,
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2)

    assert row.status == "failed"
    assert row.error_type == "LegacyWaitUnsupported"
    assert calls == []


async def test_tool_step_timeout_is_enforced(env):
    """DESIRED: step.timeout bounds a tool_call step (fail loud at T).
    BASELINE: the runner awaited the handler bare. Plan 035 enforces the
    bound and records the uncertain effect."""
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("bounded", [
        {"id": "s1", "kind": "tool_call", "tool": "slow", "args": {},
         "timeout": 1},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2.5)

    assert row.status == "timed_out_unknown", (
        f"step declared timeout=1s but the run is still '{row.status}' after "
        "2.5s — tool_call timeouts are not enforced"
    )
    assert row.error_type == "OutcomeUnknown"
    async with sf() as s:
        step = (await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
        )).scalars().first()
    assert "timeout" in (step.error or "").lower() or "timed out" in (
        step.error or "").lower()


async def test_timed_out_tool_does_not_replay_an_uncertain_effect(env):
    """A timed-out external write may have committed before cancellation."""
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("bounded-retry", [
        {"id": "s1", "kind": "tool_call", "tool": "slow", "args": {},
         "timeout": 1, "retry": {"max": 2, "backoff_seconds": 0}},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2.5)

    assert row.status == "timed_out_unknown"
    assert row.error_type == "OutcomeUnknown"
    assert calls.count("slow-started") == 1
    assert "effect may have committed" in (row.error or "").lower()


async def test_timed_out_tool_ignores_continue_and_stops_downstream_effect(env):
    """Unknown external outcome cannot be handled as a deterministic error."""
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("bounded-continue", [
        {"id": "uncertain", "kind": "tool_call", "tool": "slow", "args": {},
         "timeout": 1, "retry": {"max": 2, "backoff_seconds": 0},
         "on_error": "continue"},
        {"id": "after", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2.5)

    assert row.status == "timed_out_unknown"
    assert row.error_type == "OutcomeUnknown"
    assert calls == ["slow-started"]
    async with sf() as s:
        steps = (await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
        )).scalars().all()
    assert [(step.step_id, step.status) for step in steps] == [
        ("uncertain", "timed_out_unknown")
    ]


@pytest.mark.parametrize("container", [
    {"id": "branch", "kind": "condition", "when": "true", "on_error": "continue",
     "then": [{"id": "uncertain", "kind": "tool_call", "tool": "slow",
               "args": {}, "timeout": 1}]},
    {"id": "parallel", "kind": "parallel", "on_error": "continue",
     "branches": [[{"id": "uncertain", "kind": "tool_call", "tool": "slow",
                    "args": {}, "timeout": 1}]]},
])
async def test_nested_timeout_stops_outer_flow(env, container):
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("nested-timeout", [
        container,
        {"id": "after", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2.5)

    assert row.status == "timed_out_unknown"
    assert row.error_type == "OutcomeUnknown"
    assert calls == ["slow-started"]


async def test_uncertain_subtask_stops_parent_before_next_effect(env):
    sf, tools, runner, calls, gate = env
    await _save(sf, _playbook("child-timeout", [
        {"id": "uncertain", "kind": "tool_call", "tool": "slow", "args": {},
         "timeout": 1},
    ]))
    parent = await _save(sf, _playbook("parent-after-timeout", [
        {"id": "child", "kind": "subtask", "playbook": "child-timeout",
         "on_error": "continue"},
        {"id": "after", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    run = await runner.start_run_background(parent, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2.5)

    assert row.status == "timed_out_unknown"
    assert row.error_type == "OutcomeUnknown"
    assert calls == ["slow-started"]
