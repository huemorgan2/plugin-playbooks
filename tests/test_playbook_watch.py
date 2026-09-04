"""plans/029 — playbook_watch: one-shot 'wake me when this playbook's next
run finishes'.

Double-trigger rules under test (017 standing rule 1): per (run,
conversation) at most ONE moment — launcher moment, inline agent result,
and the ops fix-proposal failure moment all suppress the watch moment for
their own conversation (watch consumed silently).
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookWatch
from plugin_playbooks.wake import RunCompletionWake

OPS_ID = uuid.uuid4()


class _Bus:
    def __init__(self) -> None:
        self.events = []

    async def emit(self, name, payload):
        self.events.append((name, payload))

    def subscribe(self, name, handler, **_kw):
        return lambda: None


class _Ctx:
    def __init__(self, conv=None) -> None:
        self.sent: list[dict] = []
        self.current_conversation_id = conv

    async def send_muted_message(self, title, content, **kw):
        self.sent.append({"title": title, "content": content, **kw})
        return {"responded": True}

    async def ops_conversation_id(self):
        return OPS_ID


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await engine.dispose()


async def _save_pb(sf, name="watched-pb") -> Playbook:
    pb = Playbook(
        name=name,
        display_name=name,
        definition={"name": name, "steps": []},
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


def _payload(pb, **over):
    base = {
        "run_id": str(uuid.uuid4()),
        "status": "done",
        "duration_ms": 1000,
        "error": None,
        "playbook_id": str(pb.id),
        "playbook_version": 1,
        "is_test": False,
        "playbook_name": pb.name,
        "trigger": "webhook",
        "conversation_id": None,
        "parent_run_id": None,
        "wake_on_complete": False,
    }
    base.update(over)
    return base


async def _drain(svc):
    while svc._tasks:
        await asyncio.gather(*list(svc._tasks), return_exceptions=True)


async def _watch_rows(sf):
    async with sf() as s:
        return (await s.execute(select(PlaybookWatch))).scalars().all()


# --- tool contract -----------------------------------------------------------


async def test_watch_tool_stamps_conversation_and_replies_end_turn(env):
    sf = env
    pb = await _save_pb(sf)
    conv = uuid.uuid4()
    tools = build_tools(sf, _Bus(), None, _Ctx(conv=conv))
    out = json.loads(await _handler(tools, "playbook_watch")(
        name=pb.name, note="resume the migration",
    ))
    assert out["watching"] == pb.name
    assert "WOKEN" in out["message"] and "end" in out["message"].lower()
    rows = await _watch_rows(sf)
    assert len(rows) == 1
    assert rows[0].conversation_id == conv
    assert rows[0].note == "resume the migration"
    assert rows[0].consumed_at is None


async def test_rewatch_refreshes_instead_of_duplicating(env):
    sf = env
    pb = await _save_pb(sf)
    conv = uuid.uuid4()
    tools = build_tools(sf, _Bus(), None, _Ctx(conv=conv))
    await _handler(tools, "playbook_watch")(name=pb.name)
    await _handler(tools, "playbook_watch")(name=pb.name, note="second")
    rows = await _watch_rows(sf)
    assert len(rows) == 1
    assert rows[0].note == "second"


async def test_watch_cancel(env):
    sf = env
    pb = await _save_pb(sf)
    conv = uuid.uuid4()
    tools = build_tools(sf, _Bus(), None, _Ctx(conv=conv))
    await _handler(tools, "playbook_watch")(name=pb.name)
    out = json.loads(await _handler(tools, "playbook_watch_cancel")(name=pb.name))
    assert out["cancelled"] is True
    assert await _watch_rows(sf) == []


# --- delivery ----------------------------------------------------------------


async def _add_watch(sf, pb, conv, days=7, note=None):
    async with sf() as s:
        w = PlaybookWatch(
            playbook_id=pb.id,
            conversation_id=conv,
            note=note,
            expires_at=datetime.now(timezone.utc) + timedelta(days=days),
        )
        s.add(w)
        await s.commit()
        await s.refresh(w)
    return w


async def test_background_completion_wakes_watcher_once(env):
    sf = env
    pb = await _save_pb(sf)
    watcher = uuid.uuid4()
    await _add_watch(sf, pb, watcher, note="waiting on the nightly sync")
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    await svc._on_completed(_payload(pb))
    await _drain(svc)
    moments = [m for m in ctx.sent if m.get("channel") == "moment"]
    assert len(moments) == 1
    assert moments[0]["conversation_id"] == watcher
    assert "nightly sync" in moments[0]["content"]
    rows = await _watch_rows(sf)
    assert rows[0].consumed_at is not None
    # a second completion: watch already consumed → no new moment
    await svc._on_completed(_payload(pb))
    await _drain(svc)
    assert len([m for m in ctx.sent if m.get("channel") == "moment"]) == 1


async def test_launcher_conversation_gets_single_moment(env):
    sf = env
    pb = await _save_pb(sf)
    conv = uuid.uuid4()
    await _add_watch(sf, pb, conv)
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    await svc._on_completed(_payload(
        pb, trigger="agent", wake_on_complete=True,
        conversation_id=str(conv),
    ))
    await _drain(svc)
    moments = [m for m in ctx.sent if m.get("channel") == "moment"]
    assert len(moments) == 1  # the 028 launcher moment only
    assert "you started earlier" in moments[0]["content"]
    rows = await _watch_rows(sf)
    assert rows[0].consumed_at is not None  # consumed silently


async def test_inline_agent_run_same_conversation_is_silent(env):
    sf = env
    pb = await _save_pb(sf)
    conv = uuid.uuid4()
    await _add_watch(sf, pb, conv)
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    await svc._on_completed(_payload(
        pb, trigger="agent", conversation_id=str(conv),
    ))
    await _drain(svc)
    assert [m for m in ctx.sent if m.get("channel") == "moment"] == []
    rows = await _watch_rows(sf)
    assert rows[0].consumed_at is not None


async def test_ops_watcher_yields_to_fix_proposal_on_failure(env):
    sf = env
    pb = await _save_pb(sf)
    await _add_watch(sf, pb, OPS_ID)
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    await svc._on_completed(_payload(pb, status="failed", error="boom"))
    await _drain(svc)
    # No watch moment: FixProposalService owns background failures in ops.
    # (The awareness note is channel="awareness", not a turn.)
    assert [m for m in ctx.sent if m.get("channel") == "moment"] == []
    rows = await _watch_rows(sf)
    assert rows[0].consumed_at is not None


async def test_non_ops_watcher_gets_failure_moment(env):
    sf = env
    pb = await _save_pb(sf)
    watcher = uuid.uuid4()
    await _add_watch(sf, pb, watcher)
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    await svc._on_completed(_payload(pb, status="failed", error="boom"))
    await _drain(svc)
    moments = [m for m in ctx.sent if m.get("channel") == "moment"]
    assert len(moments) == 1
    assert moments[0]["conversation_id"] == watcher
    assert "boom" in moments[0]["content"]


async def test_expired_watch_is_reaped_without_moment(env):
    sf = env
    pb = await _save_pb(sf)
    await _add_watch(sf, pb, uuid.uuid4(), days=-1)
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    await svc._on_completed(_payload(pb))
    await _drain(svc)
    assert [m for m in ctx.sent if m.get("channel") == "moment"] == []
    assert await _watch_rows(sf) == []


async def test_concurrent_completions_consume_once(env):
    sf = env
    pb = await _save_pb(sf)
    watcher = uuid.uuid4()
    await _add_watch(sf, pb, watcher)
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    await asyncio.gather(
        svc._on_completed(_payload(pb)),
        svc._on_completed(_payload(pb)),
    )
    await _drain(svc)
    assert len([m for m in ctx.sent if m.get("channel") == "moment"]) == 1


async def test_test_and_subtask_runs_never_fire_watches(env):
    sf = env
    pb = await _save_pb(sf)
    await _add_watch(sf, pb, uuid.uuid4())
    ctx = _Ctx()
    svc = RunCompletionWake(sf, _Bus(), ctx)
    await svc._on_completed(_payload(pb, is_test=True))
    await svc._on_completed(_payload(pb, parent_run_id=str(uuid.uuid4())))
    await _drain(svc)
    assert ctx.sent == []
    rows = await _watch_rows(sf)
    assert rows[0].consumed_at is None  # still armed for a real run
