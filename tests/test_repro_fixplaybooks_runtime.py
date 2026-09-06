"""RED reproduction — fix-playbooks stage 1, runtime durability holes
(luna-fixer plans/2026-09-06-fix-playbooks §1).

Four v1 runner facts these tests pin as DEFECTS (assertions state the
desired v2 behavior, so every test FAILS on current code):

1. No resume: a restart turns every in-flight run into "failed"
   (sweep_orphaned_runs, runner.py:317-372).
2. wait_for_approval AUTO-APPROVES (runner.py:1060-1069) — the gate the
   language promises does not exist.
3. wait_for_event returns a stub immediately (runner.py:1071-1083) — the
   wait the language promises does not exist.
4. step.timeout on tool_call steps is never enforced (runner.py:868-878
   awaits the handler bare; only code steps pass a timeout down).
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


async def test_interrupted_run_survives_restart_instead_of_failing(env):
    """DESIRED (v2 segmented replay): a run in flight when the process dies
    is resumed by the next process, not stamped failed. CURRENT: the new
    process's sweep_orphaned_runs marks it failed ('interrupted — the server
    restarted...') and the work is lost."""
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
    assert row.status != "failed", (
        "restart killed the run: sweep_orphaned_runs stamped it 'failed' "
        "('interrupted — the server restarted...') — v1 has no resume; the "
        "run must survive a restart and continue"
    )


async def test_wait_for_approval_actually_gates(env):
    """DESIRED: a wait_for_approval step parks the run until a real decision
    arrives; downstream steps must not execute unapproved. CURRENT: the
    runner auto-approves ({'approved': True, 'auto': True}) and sails on."""
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("gated", [
        {"id": "gate", "kind": "wait_for_approval", "show": ["summary"]},
        {"id": "after", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2)

    assert "fast" not in calls, (
        "the step AFTER wait_for_approval executed with no approval decision "
        "— the gate auto-approved"
    )
    assert row.status != "done", (
        "run completed straight through a wait_for_approval with nobody "
        "approving anything"
    )


async def test_wait_for_event_actually_waits(env):
    """DESIRED: wait_for_event parks until a matching bus event (or its
    timeout). CURRENT: it returns {'received': False, 'stub': True}
    immediately — no event was ever emitted, yet the run completes."""
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("event-waiter", [
        {"id": "w", "kind": "wait_for_event", "event": "email.received"},
        {"id": "after", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2)

    assert "fast" not in calls, (
        "the step AFTER wait_for_event executed though 'email.received' was "
        "never emitted — the wait is a stub"
    )
    assert row.status != "done", (
        "run completed without the event it claims to wait for"
    )


async def test_tool_step_timeout_is_enforced(env):
    """DESIRED: step.timeout bounds a tool_call step (fail loud at T).
    CURRENT: the runner awaits the handler bare — timeout is honored only
    for code steps, so a hung tool hangs the run forever."""
    sf, tools, runner, calls, gate = env
    pb = await _save(sf, _playbook("bounded", [
        {"id": "s1", "kind": "tool_call", "tool": "slow", "args": {},
         "timeout": 1},
    ]))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2.5)

    assert row.status == "failed", (
        f"step declared timeout=1s but the run is still '{row.status}' after "
        "2.5s — tool_call timeouts are not enforced"
    )
    async with sf() as s:
        step = (await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
        )).scalars().first()
    assert "timeout" in (step.error or "").lower() or "timed out" in (
        step.error or "").lower()
