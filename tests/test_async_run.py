"""plans/009 — background playbook runs.

The old start_run blocked to completion, so playbook_run timed out at 120s on
slow playbooks (no run_id, orphaned run) and cancel_run only flipped a DB flag
while every remaining step kept executing. These tests pin the new contract:
start_run_background returns immediately, wait_for_run bounds the wait without
killing the run, and cancel_run actually stops execution.
"""

import asyncio
import uuid

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

    def get(self, name: str) -> _Tool:
        return self._tools[name]


def _playbook(name: str, steps: list[dict]) -> Playbook:
    return Playbook(
        name=name,
        display_name=name,
        definition={"name": name, "steps": steps},
        status="enabled",
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
        await gate.wait()  # test decides when (whether) the step finishes
        calls.append("slow-finished")
        return {"ok": True}

    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(fast=_Tool(fast_tool), slow=_Tool(slow_tool)),
        events=_Bus(),
    )
    yield sf, runner, calls, gate
    gate.set()  # release any parked run so tasks don't leak across tests
    await asyncio.sleep(0)
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


async def test_background_run_returns_immediately_then_completes(env):
    sf, runner, calls, gate = env
    pb = await _save(sf, _playbook("slow-pb", [
        {"id": "s1", "kind": "tool_call", "tool": "slow", "args": {}},
    ]))

    run = await runner.start_run_background(pb, inputs={})
    # returned while the step is still parked on the gate
    assert run.status == "running"
    assert (await _run_row(sf, run.id)).status == "running"

    gate.set()
    done = await runner.wait_for_run(run.id, timeout=5)
    assert done.status == "done"
    assert calls == ["slow-started", "slow-finished"]


async def test_wait_for_run_times_out_without_killing_the_run(env):
    sf, runner, calls, gate = env
    pb = await _save(sf, _playbook("slow-pb", [
        {"id": "s1", "kind": "tool_call", "tool": "slow", "args": {}},
    ]))

    run = await runner.start_run_background(pb, inputs={})
    still = await runner.wait_for_run(run.id, timeout=0.05)
    assert still.status == "running"  # timed out, run untouched

    gate.set()
    done = await runner.wait_for_run(run.id, timeout=5)
    assert done.status == "done"
    assert calls[-1] == "slow-finished"


async def test_fast_run_finishes_within_wait_window(env):
    sf, runner, calls, _gate = env
    pb = await _save(sf, _playbook("fast-pb", [
        {"id": "s1", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))

    run = await runner.start_run_background(pb, inputs={})
    done = await runner.wait_for_run(run.id, timeout=5)
    assert done.status == "done"
    assert calls == ["fast"]

    async with sf() as s:
        steps = (await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
        )).scalars().all()
    assert [st.status for st in steps] == ["done"]
    assert steps[0].outputs["result"] == {"ok": True}


async def test_cancel_actually_stops_execution(env):
    sf, runner, calls, _gate = env
    # two steps: cancel while step 1 is parked — step 2 must never run
    pb = await _save(sf, _playbook("cancel-pb", [
        {"id": "s1", "kind": "tool_call", "tool": "slow", "args": {}},
        {"id": "s2", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))

    run = await runner.start_run_background(pb, inputs={})
    while "slow-started" not in calls:
        await asyncio.sleep(0.01)

    await runner.cancel_run(run.id)
    ended = await runner.wait_for_run(run.id, timeout=5)
    assert ended.status == "cancelled"
    assert ended.completed_at is not None
    assert "fast" not in calls  # step 2 never executed
    assert "slow-finished" not in calls  # step 1 was interrupted, not drained


async def test_blocking_start_run_unchanged_for_subtasks(env):
    sf, runner, calls, _gate = env
    pb = await _save(sf, _playbook("fast-pb", [
        {"id": "s1", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))

    run = await runner.start_run(pb, inputs={})
    # blocking form returns only after the run reached a terminal status
    assert run.status == "done"
    assert calls == ["fast"]


async def test_cancel_run_without_task_falls_back_to_db_flag(env):
    sf, runner, _calls, _gate = env
    pb = await _save(sf, _playbook("orphan-pb", [
        {"id": "s1", "kind": "tool_call", "tool": "fast", "args": {}},
    ]))
    async with sf() as s:
        orphan = PlaybookRun(
            playbook_id=pb.id, playbook_version=1, status="running",
        )
        s.add(orphan)
        await s.commit()
        await s.refresh(orphan)

    await runner.cancel_run(orphan.id)
    row = await _run_row(sf, orphan.id)
    assert row.status == "cancelled"


async def test_wait_for_run_unknown_id_returns_none(env):
    sf, runner, _calls, _gate = env
    assert await runner.wait_for_run(uuid.uuid4(), timeout=0.01) is None


async def test_sweep_marks_orphaned_runs_failed_but_spares_live_ones(env):
    sf, runner, calls, gate = env
    pb = await _save(sf, _playbook("swept-pb", [
        {"id": "s1", "kind": "tool_call", "tool": "slow", "args": {}},
    ]))

    # orphan: a "running" row (with a "running" step) no task is driving —
    # what a restart/upgrade or a pre-0.5.0 cancelled coroutine leaves behind
    async with sf() as s:
        orphan = PlaybookRun(playbook_id=pb.id, playbook_version=1, status="running")
        s.add(orphan)
        await s.flush()
        s.add(PlaybookStepRun(
            run_id=orphan.id, step_id="s1", step_kind="tool_call", status="running",
        ))
        await s.commit()
        await s.refresh(orphan)

    # live: a real background run parked on the gate — must NOT be swept
    live = await runner.start_run_background(pb, inputs={})
    while "slow-started" not in calls:
        await asyncio.sleep(0.01)

    assert await runner.sweep_orphaned_runs() == 1

    swept = await _run_row(sf, orphan.id)
    assert swept.status == "failed"
    assert swept.completed_at is not None
    async with sf() as s:
        step = (await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == orphan.id)
        )).scalars().one()
    assert step.status == "failed"
    assert "interrupted" in step.error

    assert (await _run_row(sf, live.id)).status == "running"
    gate.set()
    done = await runner.wait_for_run(live.id, timeout=5)
    assert done.status == "done"
