"""plans/028 — wake-on-completion.

playbook_run promises a wake instead of demanding polls when a run outlives
its wait window; RunCompletionWake delivers a moment to the originating
conversation on completion, background runs leave awareness rows in ops, and
the orphan sweep honors the promise across restarts.
"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.wake import RunCompletionWake

OPS_ID = uuid.uuid4()


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def subscribe(self, name, handler, **_kw):
        return lambda: None


class _Ctx:
    """089-capable core: has send_muted_message + ops chat."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_muted_message(self, title, content, **kw):
        self.sent.append({"title": title, "content": content, **kw})
        return {"responded": True}

    async def ops_conversation_id(self):
        return OPS_ID


class _Tool:
    def __init__(self, handler) -> None:
        self.handler = handler


class _Tools:
    def __init__(self, **tools) -> None:
        self._tools = tools

    def get(self, name: str) -> _Tool:
        return self._tools[name]


@pytest.fixture
async def env():
    # StaticPool: concurrent sessions (background run + the stamp write)
    # must share ONE in-memory database, not get a fresh empty one each.
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    gate = asyncio.Event()

    async def fast_tool(**_kw):
        return {"ok": True}

    async def slow_tool(**_kw):
        await gate.wait()
        return {"ok": True}

    bus = _Bus()
    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(fast=_Tool(fast_tool), slow=_Tool(slow_tool)),
        events=bus,
    )
    yield sf, runner, bus, gate
    gate.set()
    await asyncio.sleep(0)
    await engine.dispose()


async def _save_pb(sf, name, tool="fast") -> Playbook:
    pb = Playbook(
        name=name,
        display_name=name,
        definition={"name": name, "steps": [
            {"id": "s1", "kind": "tool_call", "tool": tool, "args": {}},
        ]},
        status="enabled",
        agent_autonomy="agent_may_trigger",
    )
    async with sf() as s:
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
    return pb


def _handler(tools, name):
    return next(h for d, h in tools if d.name == name)


async def _drain(svc: RunCompletionWake) -> None:
    while svc._tasks:
        await asyncio.gather(*list(svc._tasks), return_exceptions=True)


# --- the tool contract -------------------------------------------------------


async def test_slow_run_promises_wake_and_stamps_flag(env):
    sf, runner, bus, gate = env
    await _save_pb(sf, "slow-pb", tool="slow")
    ctx = _Ctx()
    tools = build_tools(sf, bus, runner, ctx)
    import json
    out = json.loads(await _handler(tools, "playbook_run")(
        name="slow-pb", wait_seconds=0,
    ))
    assert out["status"] == "running"
    assert "WOKEN" in out["message"]
    assert "poll" not in out["message"].lower().replace("do not poll", "")
    async with sf() as s:
        row = await s.get(PlaybookRun, uuid.UUID(out["run_id"]))
        assert row.wake_on_complete is True

    gate.set()
    done = await runner.wait_for_run(uuid.UUID(out["run_id"]), timeout=5)
    assert done.status == "done"
    completed = [p for n, p in bus.events if n == "playbook.run.completed"]
    assert completed and completed[-1]["wake_on_complete"] is True
    assert completed[-1]["playbook_name"] == "slow-pb"
    assert completed[-1]["trigger"] == "agent"


async def test_fast_run_returns_inline_without_flag(env):
    sf, runner, bus, gate = env
    await _save_pb(sf, "fast-pb")
    ctx = _Ctx()
    tools = build_tools(sf, bus, runner, ctx)
    import json
    out = json.loads(await _handler(tools, "playbook_run")(name="fast-pb"))
    assert out["status"] == "done"
    assert "step_results" in out
    async with sf() as s:
        row = await s.get(PlaybookRun, uuid.UUID(out["run_id"]))
        assert row.wake_on_complete is False


async def test_old_core_keeps_poll_contract(env):
    sf, runner, bus, gate = env
    await _save_pb(sf, "slow-pb", tool="slow")
    tools = build_tools(sf, bus, runner, ctx=None)  # no send_muted_message
    import json
    out = json.loads(await _handler(tools, "playbook_run")(
        name="slow-pb", wait_seconds=0,
    ))
    assert out["status"] == "running"
    assert "Poll playbook_status" in out["message"]
    async with sf() as s:
        row = await s.get(PlaybookRun, uuid.UUID(out["run_id"]))
        assert row.wake_on_complete is False


# --- the wake service --------------------------------------------------------


def _payload(**over):
    base = {
        "run_id": str(uuid.uuid4()),
        "status": "done",
        "duration_ms": 61000,
        "error": None,
        "playbook_id": str(uuid.uuid4()),
        "playbook_version": 3,
        "is_test": False,
        "playbook_name": "my-pb",
        "trigger": "agent",
        "conversation_id": None,
        "parent_run_id": None,
        "wake_on_complete": False,
    }
    base.update(over)
    return base


@pytest.fixture
async def svc_env():
    # StaticPool: concurrent sessions (background run + the stamp write)
    # must share ONE in-memory database, not get a fresh empty one each.
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    yield sf, ctx, svc
    await engine.dispose()


async def test_flagged_done_run_wakes_origin_with_outputs(svc_env):
    sf, ctx, svc = svc_env
    origin = uuid.uuid4()
    run_id = uuid.uuid4()
    async with sf() as s:
        s.add(PlaybookStepRun(
            run_id=run_id, step_id="s1", step_kind="tool_call",
            status="done", outputs={"answer": 42},
        ))
        await s.commit()
    await svc._on_completed(_payload(
        run_id=str(run_id), wake_on_complete=True,
        conversation_id=str(origin),
    ))
    await _drain(svc)
    assert len(ctx.sent) == 1
    msg = ctx.sent[0]
    assert msg["channel"] == "moment"
    assert msg["conversation_id"] == origin
    assert "my-pb" in msg["content"] and "'done'" in msg["content"]
    assert "answer" in msg["content"]
    assert msg["tools"] == "all"


async def test_flagged_failed_run_wakes_with_error(svc_env):
    sf, ctx, svc = svc_env
    await svc._on_completed(_payload(
        wake_on_complete=True, status="failed", error="step s1 blew up",
    ))
    await _drain(svc)
    assert len(ctx.sent) == 1
    msg = ctx.sent[0]
    assert msg["channel"] == "moment"
    assert msg["conversation_id"] == OPS_ID  # no origin -> ops fallback
    assert "step s1 blew up" in msg["content"]
    assert "fabricate" in msg["content"]


async def test_unflagged_agent_run_is_silent(svc_env):
    sf, ctx, svc = svc_env
    await svc._on_completed(_payload(trigger="agent"))
    await svc._on_completed(_payload(trigger="agent-candidate"))
    await _drain(svc)
    assert ctx.sent == []


async def test_background_run_leaves_awareness_row(svc_env):
    sf, ctx, svc = svc_env
    await svc._on_completed(_payload(trigger="monday.item.created"))
    await _drain(svc)
    assert len(ctx.sent) == 1
    msg = ctx.sent[0]
    assert msg["channel"] == "awareness"
    assert msg["respond"] is False
    assert msg["conversation_id"] == OPS_ID


async def test_test_and_subtask_runs_are_silent(svc_env):
    sf, ctx, svc = svc_env
    await svc._on_completed(_payload(is_test=True, wake_on_complete=True))
    await svc._on_completed(_payload(
        trigger="subtask:xyz", parent_run_id=str(uuid.uuid4()),
    ))
    await _drain(svc)
    assert ctx.sent == []


async def test_old_core_without_muted_is_a_noop(svc_env):
    sf, _ctx, _svc = svc_env

    class _Bare:
        pass

    svc = RunCompletionWake(sf, _Bus(), _Bare())
    await svc._on_completed(_payload(wake_on_complete=True))
    await _drain(svc)  # no crash is the assertion


# --- restart safety ----------------------------------------------------------


async def test_sweep_emits_completion_for_flagged_orphans_only(env):
    sf, runner, bus, gate = env
    pb = await _save_pb(sf, "orphan-pb")
    async with sf() as s:
        flagged = PlaybookRun(
            playbook_id=pb.id, playbook_version=1, status="running",
            trigger="agent", wake_on_complete=True,
        )
        quiet = PlaybookRun(
            playbook_id=pb.id, playbook_version=1, status="running",
            trigger="monday.item.created", wake_on_complete=False,
        )
        s.add_all([flagged, quiet])
        await s.commit()
        flagged_id, quiet_id = flagged.id, quiet.id

    swept = await runner.sweep_orphaned_runs()
    assert swept == 2
    async with sf() as s:
        assert (await s.get(PlaybookRun, flagged_id)).status == "failed"
        assert (await s.get(PlaybookRun, quiet_id)).status == "failed"
    completed = [p for n, p in bus.events if n == "playbook.run.completed"]
    assert len(completed) == 1
    assert completed[0]["run_id"] == str(flagged_id)
    assert completed[0]["wake_on_complete"] is True
    assert "interrupted" in (completed[0]["error"] or "")
