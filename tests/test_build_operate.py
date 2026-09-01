"""0.26.0 (plans/015, 089 build/operate) — the new contracts.

- publish test-run gate: refuses untested and red candidates, passes green;
- report_to stamping at run creation (never delivery-time resolution);
- test runs excluded from the production failure digest;
- trigger single-flight dedupe;
- mode declarations on tool defs (active once the SDK carries `modes`);
- one open fix proposal per (playbook, failure signature).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from evidence import EXPLANATION, green_run, make_plan
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import failure_digest
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.fix_proposals import FixProposalService, failure_signature
from plugin_playbooks.models import (
    Base,
    Playbook,
    PlaybookFixProposal,
    PlaybookRun,
    PlaybookStepRun,
)
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.triggers import PlaybookTriggerService


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.handlers: dict[str, list] = {}

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))
        for h in self.handlers.get(name, []):
            await h(payload)

    def subscribe(self, name: str, handler, background: bool = False):
        self.handlers.setdefault(name, []).append(handler)
        return lambda: self.handlers[name].remove(handler)


class _FakeRun:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.status = "done"


class _StubRunner:
    _tools = None
    _agent = None

    def __init__(self) -> None:
        self.started: list = []

    async def dry_run(self, playbook, inputs=None):
        return {"ok": True}

    async def start_run_background(
        self, playbook, inputs=None, trigger=None, is_test=False,
    ):
        self.started.append((playbook, trigger, is_test))
        return _FakeRun()

    async def wait_for_run(self, run_id, timeout=None):
        return None


CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")

OPS = uuid.uuid4()
ORIGIN = uuid.uuid4()


class _Ctx:
    """Fake 089 core surface: origin contextvar + ops chat discovery."""

    def __init__(self, origin=None, ops=OPS) -> None:
        self.current_conversation_id = origin
        self._ops = ops

    async def ops_conversation_id(self):
        return self._ops


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    bus = _Bus()
    runner = _StubRunner()
    tools = {td.name: h for td, h in build_tools(sf, bus, runner)}
    yield sf, tools, runner, bus
    await engine.dispose()


async def _make_candidate(tools) -> None:
    await tools["playbook_propose"](name="greeter", code=CODE)
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"], code=NEW_CODE,
    )


# --- the test-run gate (089 contract #8) ------------------------------------

@pytest.mark.asyncio
async def test_publish_refused_without_test_run(env):
    sf, tools, _, _ = env
    await _make_candidate(tools)
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert out["gate"] == "test_run"
    assert "simulations" in out["error"]
    assert "playbook_run_candidate" in out["hint"]
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
    assert pb.live_version == 1                 # nothing went live
    assert pb.candidate_version == 2


@pytest.mark.asyncio
async def test_publish_refused_on_failed_test_run(env):
    sf, tools, _, _ = env
    await _make_candidate(tools)
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
        s.add(PlaybookRun(
            playbook_id=pb.id, playbook_version=2, status="failed",
            trigger="agent-candidate", is_test=True, started_at=later,
            completed_at=later,
        ))
        await s.commit()
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert out["gate"] == "test_run"
    assert "FAILED" in out["error"]
    assert out["run_id"]


@pytest.mark.asyncio
async def test_publish_passes_with_green_test_run_and_announces(env):
    sf, tools, _, bus = env
    await _make_candidate(tools)
    await green_run(sf, 2)
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert out["status"] == "published"
    gates = {g["gate"]: g for g in out["gates"]}
    assert gates["test_run"]["ok"] is True
    assert gates["test_run"]["run_id"]
    published = [p for n, p in bus.events if n == "playbook.published"]
    assert published and published[0]["action"] == "publish"
    assert published[0]["new_version"] == 2
    assert published[0]["evidence_run_id"] == gates["test_run"]["run_id"]


@pytest.mark.asyncio
async def test_stale_evidence_does_not_satisfy_a_new_edit(env):
    """A green run of v2 is no evidence for v3 — every edit needs a fresh
    test (version rows are immutable, so version identity carries it)."""
    sf, tools, _, _ = env
    await _make_candidate(tools)
    await green_run(sf, 2)
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"],
        old="inputs.name", new="inputs.nickname",
    )
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert out["gate"] == "test_run"


# --- report_to stamping (089 §1) --------------------------------------------

def _real_runner(sf, ctx) -> PlaybookRunner:
    return PlaybookRunner(
        session_factory=sf, tool_registry=None, events=_Bus(), context=ctx,
    )


async def _seed_playbook(sf) -> Playbook:
    async with sf() as s:
        pb = Playbook(
            name="greeter", description="says hi", status="enabled",
            definition={"name": "greeter", "steps": []}, version=1,
            live_version=1,
        )
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
        return pb


@pytest.mark.asyncio
async def test_report_to_stamping_rules():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    pb = await _seed_playbook(sf)

    # test run with an origin chat → reports to that chat
    r = await _real_runner(sf, _Ctx(origin=ORIGIN))._create_run(
        pb, trigger="agent-candidate", is_test=True,
    )
    assert r.report_to == ORIGIN and r.is_test is True
    # test run without an origin (headless) → ops
    r = await _real_runner(sf, _Ctx())._create_run(pb, is_test=True)
    assert r.report_to == OPS
    # live run the agent starts inside a chat → that chat (the exception)
    r = await _real_runner(sf, _Ctx(origin=ORIGIN))._create_run(
        pb, trigger="agent",
    )
    assert r.report_to == ORIGIN and r.is_test is False
    # plans/016 phase 2: background live runs NEVER report to the ops chat —
    # a bus trigger with a leaked origin stamps nothing
    r = await _real_runner(sf, _Ctx(origin=ORIGIN))._create_run(
        pb, trigger="some.bus.event",
    )
    assert r.report_to is None
    # cron/live run, no origin → nothing (send steps must name their chat)
    r = await _real_runner(sf, _Ctx())._create_run(pb, trigger="schedule")
    assert r.report_to is None
    # subtask trigger inside a chat behaves like "agent"
    r = await _real_runner(sf, _Ctx(origin=ORIGIN))._create_run(
        pb, trigger="subtask:parent",
    )
    assert r.report_to == ORIGIN
    await engine.dispose()


# --- digest excludes test runs (089 §1) -------------------------------------

@pytest.mark.asyncio
async def test_failure_digest_ignores_test_runs():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    pb = await _seed_playbook(sf)
    now = datetime.now(timezone.utc)
    async with sf() as s:
        s.add(PlaybookRun(
            playbook_id=pb.id, playbook_version=1, status="failed",
            is_test=True, started_at=now, completed_at=now,
        ))
        await s.commit()
        assert await failure_digest(s) == []    # test failures don't count
        s.add(PlaybookRun(
            playbook_id=pb.id, playbook_version=1, status="failed",
            is_test=False, started_at=now, completed_at=now,
        ))
        await s.commit()
        digest = await failure_digest(s)
    assert len(digest) == 1 and digest[0]["failed"] == 1
    await engine.dispose()


# --- trigger single-flight (089 §6) -----------------------------------------

class _SlowRunner:
    def __init__(self) -> None:
        self.started: list = []
        self._tasks: dict = {}
        self.release = asyncio.Event()

    async def start_run_background(self, playbook, inputs=None, trigger=None):
        self.started.append(trigger)
        run = _FakeRun()
        self._tasks[run.id] = asyncio.create_task(self.release.wait())
        return run


@pytest.mark.asyncio
async def test_identical_concurrent_trigger_deliveries_dedupe():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as s:
        s.add(Playbook(
            name="greeter", description="says hi", status="enabled",
            version=1, live_version=1,
            definition={
                "name": "greeter",
                "steps": [],
                "triggers": [{"event": "email.received"}],
            },
        ))
        await s.commit()
    bus = _Bus()
    runner = _SlowRunner()
    svc = PlaybookTriggerService(session_factory=sf, events=bus, runner=runner)
    await svc.start()

    payload = {"from": "x@y.z"}
    await bus.emit("email.received", payload)
    await bus.emit("email.received", payload)   # identical, run still alive
    assert len(runner.started) == 1             # deduped

    runner.release.set()                        # first run finishes
    await asyncio.gather(*runner._tasks.values())
    await asyncio.sleep(0)                      # let done_callbacks run
    await bus.emit("email.received", payload)
    assert len(runner.started) == 2             # key cleared, fires again

    await bus.emit("email.received", {"from": "other@y.z"})
    assert len(runner.started) == 3             # different inputs never dedupe
    await svc.stop()
    await engine.dispose()


# --- mode declarations (089 §5) ---------------------------------------------

def test_mode_declarations():
    tds = {td.name: td for td, _ in build_tools(None, _Bus(), _StubRunner())}
    # luna 098: the ops mode machine is gone — the only states are planning
    # and building. A tool either opts into planning (["planning",
    # "building"]) or leaves modes undeclared (= building only). No tool may
    # reference a dead ops state.
    planning_and_building = ["planning", "building"]
    for name, td in tds.items():
        modes = getattr(td, "modes", None)
        assert modes in (None, planning_and_building), (name, modes)
    # plans/016: publish/rollback and the plan tools are available even in
    # planning — the plan gate is the enforcement, not modes.
    planning_ok = {
        n for n, td in tds.items()
        if getattr(td, "modes", None) == planning_and_building
    }
    assert planning_ok == {
        "playbook_list", "playbook_status", "playbook_cancel",
        "playbook_get_definition", "playbook_validate",
        "playbook_language_reference", "playbook_spec_list",
        "playbook_spec_from_run", "playbook_preflight",
        "playbook_plan_write", "playbook_plan_read", "playbook_plan_finish",
        "playbook_publish", "playbook_rollback",
    }
    # everything else (draft authoring, runs, autonomy, acks) is
    # building-only by default — absent while the owner is planning.
    for name in ("playbook_propose", "playbook_edit", "playbook_edit_force",
                 "playbook_manifest_set", "playbook_spec_add",
                 "playbook_spec_delete", "playbook_spec_run",
                 "playbook_dry_run", "playbook_run_candidate",
                 "playbook_run", "playbook_set_autonomy",
                 "playbook_ack_failures"):
        assert getattr(tds[name], "modes", None) is None, name


def test_artifact_ref_declarations():
    """089 P5 work registry: mutating playbook tools claim playbook:{name}."""
    tds = {td.name: td for td, _ in build_tools(None, _Bus(), _StubRunner())}
    for name in ("playbook_propose", "playbook_edit", "playbook_edit_force",
                 "playbook_manifest_set", "playbook_publish",
                 "playbook_rollback", "playbook_run_candidate",
                 "playbook_spec_add", "playbook_spec_delete"):
        assert getattr(tds[name], "artifact_ref", None) == "playbook:{name}", name
    assert getattr(tds["playbook_publish"], "artifact_verb", None) == "publishing"
    assert getattr(tds["playbook_run_candidate"], "artifact_verb", None) == "testing"
    # read/inspect tools never claim work
    for name in ("playbook_list", "playbook_status", "playbook_get_definition",
                 "playbook_validate", "playbook_spec_list"):
        assert getattr(tds[name], "artifact_ref", None) is None, name


# --- fix proposals (089 §4) -------------------------------------------------

async def _seed_failed_live_run(sf, pb) -> PlaybookRun:
    now = datetime.now(timezone.utc)
    async with sf() as s:
        run = PlaybookRun(
            playbook_id=pb.id, playbook_version=1, status="failed",
            is_test=False, started_at=now, completed_at=now,
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)
        s.add(PlaybookStepRun(
            run_id=run.id, step_id="say", step_kind="tool_call",
            status="failed",
            error="HTTP 500 from mail server (attempt 3)", started_at=now,
        ))
        await s.commit()
        return run


@pytest.mark.asyncio
async def test_fix_proposal_filed_once_per_signature():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    pb = await _seed_playbook(sf)
    svc = FixProposalService(sf, _Bus(), ctx=None)   # ledger-only core

    run1 = await _seed_failed_live_run(sf, pb)
    await svc._file_proposal_inner({"run_id": str(run1.id), "status": "failed"})
    run2 = await _seed_failed_live_run(sf, pb)      # identical failure again
    await svc._file_proposal_inner({"run_id": str(run2.id), "status": "failed"})

    async with sf() as s:
        rows = (await s.execute(select(PlaybookFixProposal))).scalars().all()
    assert len(rows) == 1                           # one open proposal
    assert rows[0].failure_count == 2               # repeat bumped the count
    assert rows[0].last_run_id == run2.id
    assert rows[0].status == "open"
    await engine.dispose()


@pytest.mark.asyncio
async def test_fix_proposal_skips_test_runs_and_stale_versions():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    pb = await _seed_playbook(sf)
    svc = FixProposalService(sf, _Bus(), ctx=None)
    now = datetime.now(timezone.utc)

    async with sf() as s:
        test_run = PlaybookRun(
            playbook_id=pb.id, playbook_version=1, status="failed",
            is_test=True, started_at=now, completed_at=now,
        )
        stale_run = PlaybookRun(
            playbook_id=pb.id, playbook_version=99, status="failed",
            is_test=False, started_at=now, completed_at=now,
        )
        s.add_all([test_run, stale_run])
        await s.commit()
        await s.refresh(test_run)
        await s.refresh(stale_run)

    await svc._file_proposal_inner({"run_id": str(test_run.id), "status": "failed"})
    await svc._file_proposal_inner({"run_id": str(stale_run.id), "status": "failed"})
    async with sf() as s:
        rows = (await s.execute(select(PlaybookFixProposal))).scalars().all()
    assert rows == []
    await engine.dispose()


def test_failure_signature_stable_across_volatile_details():
    a = failure_signature("pb", "say", "HTTP 500 from mail server (attempt 3)")
    b = failure_signature("pb", "say", "HTTP 500 from mail server (attempt 7)")
    c = failure_signature("pb", "say", "connection refused")
    assert a == b       # digits stripped — retries collapse
    assert a != c       # different failure, different proposal


# --- fix proposals: failure wake, no card (plans/016 phase 2) ---------------

class _OpsCtx:
    """Fake core: ops chat discovery + muted-message surface + chat modes.
    plans/016 phase 2: there is NO approval engine here on purpose — the
    wake must never ask for approval (consent lives at the publish card)."""

    def __init__(self, ops=OPS) -> None:
        self.sent: list[tuple[str, str, dict]] = []
        self.state_flips: list[tuple] = []
        self._ops = ops

    async def ops_conversation_id(self):
        return self._ops

    async def send_muted_message(self, title, body, **kw):
        self.sent.append((title, body, kw))

    async def set_conversation_state(self, cid, state, *, only_from=None):
        self.state_flips.append((cid, state, only_from, len(self.sent)))
        return True


class _OldCoreOpsCtx(_OpsCtx):
    """A pre-0.91.001 core: no set_conversation_state. The wake must still
    go out (degrade visible, never break); since luna 098 the plugin never
    calls it on any core."""

    set_conversation_state = None


class _NoChatCtx:
    """A core without a muted-message surface: the proposal must survive as
    a ledger row (the failure digest surfaces it), nothing raises."""

    async def ops_conversation_id(self):
        return OPS


async def _wake_env(ctx):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    pb = await _seed_playbook(sf)
    return engine, sf, pb, FixProposalService(sf, _Bus(), ctx=ctx)


@pytest.mark.asyncio
async def test_fix_proposal_wakes_ops_without_approval():
    ctx = _OpsCtx()
    engine, sf, pb, svc = await _wake_env(ctx)
    try:
        # a manifest opening with a bare heading: the wake must show the
        # first PROSE line as the purpose, never the heading label.
        async with sf() as s:
            row = await s.get(Playbook, pb.id)
            row.manifest = "# Purpose\nGreets the owner.\n\n# Never\n- lie"
            await s.commit()
        run = await _seed_failed_live_run(sf, pb)
        await svc._file_proposal_inner({"run_id": str(run.id), "status": "failed"})

        # the ledger row stays OPEN — no card decided anything
        async with sf() as s:
            row = (await s.execute(select(PlaybookFixProposal))).scalar_one()
        assert row.status == "open"
        assert row.failure_count == 1

        # exactly one muted wake, agentic, into the ops chat
        ((title, body, kw),) = ctx.sent
        assert title == "Playbook failing: greeter"
        assert kw["conversation_id"] == OPS
        assert kw["channel"] == "moment"
        assert kw["respond"] is True
        assert kw["tools"] == "all"
        assert kw["source"] == "playbooks"
        # owner-readable: what failed, where, error, count, and steering
        assert "'greeter'" in body and "Greets the owner." in body
        assert "Purpose" not in body  # heading labels never leak
        assert "'say' step" in body
        assert "failed once" in body
        assert "HTTP 500 from mail server" in body
        assert str(run.id) in body
        assert "playbook_plan_write" in body   # plan before any change
        assert "publish approval card" in body  # where the owner decides

        # luna 098: no state juggling — the ops chat is always 'building',
        # the wake never touches conversation state.
        assert ctx.state_flips == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fix_proposal_repeat_bumps_count_and_wakes_again():
    ctx = _OpsCtx()
    engine, sf, pb, svc = await _wake_env(ctx)
    try:
        run1 = await _seed_failed_live_run(sf, pb)
        run2 = await _seed_failed_live_run(sf, pb)
        await svc._file_proposal_inner({"run_id": str(run1.id), "status": "failed"})
        await svc._file_proposal_inner({"run_id": str(run2.id), "status": "failed"})
        async with sf() as s:
            row = (await s.execute(select(PlaybookFixProposal))).scalar_one()
        assert row.status == "open"          # dedupe: still ONE row
        assert row.failure_count == 2
        assert row.last_run_id == run2.id
        assert len(ctx.sent) == 2            # each failure wakes once
        assert "failed 2 times" in ctx.sent[1][1]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fix_proposal_wake_on_old_core_without_state_api():
    ctx = _OldCoreOpsCtx()
    engine, sf, pb, svc = await _wake_env(ctx)
    try:
        run = await _seed_failed_live_run(sf, pb)
        await svc._file_proposal_inner({"run_id": str(run.id), "status": "failed"})
        ((_, _, kw),) = ctx.sent
        assert kw["tools"] == "all"
        assert ctx.state_flips == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fix_proposal_no_chat_surface_degrades_to_ledger():
    engine, sf, pb, svc = await _wake_env(_NoChatCtx())
    try:
        run = await _seed_failed_live_run(sf, pb)
        # must not raise, and the ledger row must survive as "open"
        await svc._file_proposal_inner({"run_id": str(run.id), "status": "failed"})
        async with sf() as s:
            row = (await s.execute(select(PlaybookFixProposal))).scalar_one()
        assert row.status == "open"
    finally:
        await engine.dispose()
