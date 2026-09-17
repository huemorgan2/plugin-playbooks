"""plans/034 (0.57.0; luna-fixer plans/2026-09-06-playbook-publish-verify)
— publish returns a VERIFIED live_version; the approve→wake→re-gate loop
guard; honest awaiting hint.

- success carries {"published": true, "live_version": N, "verified": true}
  where N is READ BACK from the store, plus the "Report exactly" hint;
- a forced read-back mismatch (mocked store) is an error with no
  status/published field, and nothing is announced;
- approved-then-re-gated same payload → the hard "approval flow is broken"
  error and NO second card (blind engine: the plugin's own memory);
- first-time awaiting flow unchanged (WOKEN / do-NOT-retry);
- the guard expires after REISSUE_WINDOW (30 min, P4-6);
- the guard never blocks a different version;
- still-pending re-issue (engine says pending) → awaiting again, same
  approval_id, no new card;
- an exact-payload grant hit lets the woken re-issue through.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from evidence import EXPLANATION, green_run
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import publish_guard
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def subscribe(self, name: str, handler, background: bool = False):
        return lambda: None


class _StubRunner:
    _tools = None
    _agent = None

    async def dry_run(self, playbook, inputs=None):
        return {"ok": True}


class _Decision:
    def __init__(self, decision="approved", reason=None, request_id=None):
        self.decision = decision
        self.reason = reason
        self.request_id = request_id or uuid.uuid4()


class _Request:
    def __init__(self, status):
        self.status = status


class _Grants:
    def __init__(self, hits=None):
        self.hits = hits or {}
        self.lookups: list[dict] = []

    async def lookup_detail_full(self, kind, target, payload, *, plugin=None):
        self.lookups.append(payload)
        key = json.dumps(payload, sort_keys=True)
        return ("approved", "orphan", "until") if key in self.hits else None


class _BlindApprovals:
    """No get(), no grants — the engine of the repro pin."""

    def __init__(self, decisions):
        self.requests: list[dict] = []
        self._decisions = list(decisions)

    async def request_nowait(self, **kw):
        self.requests.append(kw)
        return _Decision(self._decisions.pop(0))

    async def request(self, **kw):
        return await self.request_nowait(**kw)

    async def record_auto_approval(self, **kw):
        pass


class _Approvals(_BlindApprovals):
    """Engine with get() and grants (luna 0.92.046 shape)."""

    def __init__(self, decisions, *, statuses=None, grants=None):
        super().__init__(decisions)
        self.statuses = statuses or {}
        self.grants = grants or _Grants()

    async def get(self, request_id):
        status = self.statuses.get(str(request_id))
        return _Request(status) if status else None


OPS = uuid.uuid4()


class _Ctx:
    def __init__(self, approvals):
        self.approval = approvals

    async def ops_conversation_id(self):
        return OPS

    def conversation_state(self):
        return None


CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")
NEWER_CODE = CODE.replace("inputs.greeting", "inputs.nickname")


async def _env(approvals):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    bus = _Bus()
    tools = {td.name: h for td, h in build_tools(sf, bus, _StubRunner(), _Ctx(approvals))}
    return engine, sf, tools, bus


async def _pb(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


async def _seed_live_v1(sf, tools) -> None:
    """v1 live without a card (the engines below may answer pending)."""
    await tools["playbook_propose"](name="greeter", code=CODE)
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
        pb.live_version, pb.candidate_version = 1, None
        await s.commit()


async def _candidate(sf, tools, code=NEW_CODE) -> int:
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"], code=code,
    ))
    v = out["candidate_version"]
    await green_run(sf, v)
    return v


async def _publish(tools) -> dict:
    return json.loads(await tools["playbook_publish"](
        name="greeter", explanation=EXPLANATION,
    ))


# ------------------------------------------------------- verified success

@pytest.mark.asyncio
async def test_success_carries_read_back_live_version():
    approvals = _BlindApprovals(["approved"])
    engine, sf, tools, bus = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        out = await _publish(tools)

        assert out["status"] == "published"
        assert out["published"] is True
        assert out["verified"] is True
        assert out["live_version"] == 2
        assert out["hint"] == (
            "Report exactly live_version=2; do not claim any other version is live."
        )
        assert (await _pb(sf)).live_version == 2
        assert any(n == "playbook.published" for n, _ in bus.events)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_read_back_mismatch_is_an_error_and_announces_nothing(monkeypatch):
    approvals = _BlindApprovals(["approved"])
    engine, sf, tools, bus = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)

        async def _stale_store(session_factory, name):
            return 1  # the store "still" says v1

        monkeypatch.setattr(publish_guard, "read_back_live_version", _stale_store)
        bus.events.clear()
        out = await _publish(tools)

        assert "status" not in out and "published" not in out
        assert out["verified"] is False
        assert out["error"].startswith("publish reported success but live_version reads 1")
        assert "do not tell the owner it is live" in out["error"]
        assert out["stored_live_version"] == 1 and out["intended_live_version"] == 2
        assert not any(n in ("playbook.published", "playbook.saved") for n, _ in bus.events)
    finally:
        await engine.dispose()


# -------------------------------------------------------------- loop guard

@pytest.mark.asyncio
async def test_first_time_awaiting_flow_unchanged():
    approvals = _BlindApprovals(["pending"])
    engine, sf, tools, _ = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        out = await _publish(tools)

        assert out["status"] == "awaiting_owner_approval"
        assert "WOKEN" in out["hint"] and "retry" in out["hint"]
        assert "verified=true" in out["hint"]
        assert len(approvals.requests) == 1
        uuid.UUID(out["approval_id"])  # the engine's request id, echoed
        pb = await _pb(sf)
        assert pb.live_version == 1
        assert pb.last_card_action == "publish"
        assert pb.last_card_version == 2
        assert pb.last_card_approval_id == out["approval_id"]
        assert pb.last_card_raised_at is not None
        assert pb.last_card_decision is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approved_then_regated_trips_guard_without_a_second_card():
    approvals = _BlindApprovals(["pending", "pending"])
    engine, sf, tools, _ = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)
        assert first["status"] == "awaiting_owner_approval"

        # the owner approved; the woken re-issue re-gates (grant hole)
        second = await _publish(tools)

        assert len(approvals.requests) == 1
        assert second["status"] == publish_guard.BROKEN_FLOW_STATUS
        assert second["error"] == publish_guard.BROKEN_FLOW_ERROR
        assert second["approval_id"] == first["approval_id"]
        assert second["version"] == 2 and second["action"] == "publish"
        assert "Do NOT retry" in second["hint"]
        assert "pre-approved" not in second["hint"]  # no re-issue promise
        assert (await _pb(sf)).live_version == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recorded_approval_then_regated_trips_guard():
    """The bus delivered approval.decided (note_decision) and the engine
    has no grant for the payload → broken, one card only."""
    approvals = _Approvals(["pending", "pending"])
    engine, sf, tools, _ = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)
        assert await publish_guard.note_decision(
            sf, approval_id=first["approval_id"], decision="approved",
        )
        approvals.statuses[first["approval_id"]] = "approved"

        second = await _publish(tools)

        assert second["status"] == publish_guard.BROKEN_FLOW_STATUS
        assert len(approvals.requests) == 1
        assert approvals.grants.lookups == [
            {"name": "greeter", "version": 2, "action": "publish"},
        ]
        pb = await _pb(sf)
        assert pb.last_card_decision == "approved"
        assert pb.last_card_decided_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_grant_hit_lets_the_woken_reissue_through():
    payload = {"name": "greeter", "version": 2, "action": "publish"}
    grants = _Grants(hits={json.dumps(payload, sort_keys=True): True})
    approvals = _Approvals(["pending", "approved"], grants=grants)
    engine, sf, tools, _ = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)
        approvals.statuses[first["approval_id"]] = "approved"

        second = await _publish(tools)

        assert second["status"] == "published" and second["verified"] is True
        assert second["live_version"] == 2
        assert len(approvals.requests) == 2  # the engine auto-approved inline
        pb = await _pb(sf)
        assert pb.live_version == 2
        assert pb.last_card_approval_id is None  # memory spent on success
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_still_pending_reissue_returns_awaiting_without_a_new_card():
    approvals = _Approvals(["pending"])
    engine, sf, tools, _ = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)
        approvals.statuses[first["approval_id"]] = "pending"

        second = await _publish(tools)  # the agent retried against advice

        assert second["status"] == "awaiting_owner_approval"
        assert second["approval_id"] == first["approval_id"]
        assert len(approvals.requests) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_card_clears_the_guard():
    approvals = _Approvals(["pending", "approved"])
    engine, sf, tools, _ = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)
        approvals.statuses[first["approval_id"]] = "rejected"

        second = await _publish(tools)  # the owner asked for it again

        assert second["status"] == "published" and second["live_version"] == 2
        assert len(approvals.requests) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guard_expires_after_the_window():
    approvals = _BlindApprovals(["pending", "pending"])
    engine, sf, tools, _ = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)

        stale = publish_guard._now() - publish_guard.REISSUE_WINDOW - timedelta(seconds=1)
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            pb.last_card_raised_at = stale
            await s.commit()

        second = await _publish(tools)

        assert second["status"] == "awaiting_owner_approval"
        assert second["approval_id"] != first["approval_id"]
        assert len(approvals.requests) == 2  # a fresh card, normal flow
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_guard_does_not_block_a_different_version():
    approvals = _BlindApprovals(["pending", "pending"])
    engine, sf, tools, _ = await _env(approvals)
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)
        assert first["status"] == "awaiting_owner_approval"

        # the agent keeps editing: candidate v3 is a genuinely new change
        v = await _candidate(sf, tools, NEWER_CODE)
        assert v == 3
        second = await _publish(tools)

        assert second["status"] == "awaiting_owner_approval"
        assert second["approval_id"] != first["approval_id"]
        assert len(approvals.requests) == 2
        assert approvals.requests[1]["payload"]["version"] == 3
        assert (await _pb(sf)).last_card_version == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_note_decision_ignores_unknown_ids():
    engine, sf, tools, _ = await _env(_BlindApprovals([]))
    try:
        await _seed_live_v1(sf, tools)
        assert not await publish_guard.note_decision(
            sf, approval_id=str(uuid.uuid4()), decision="approved",
        )
    finally:
        await engine.dispose()


# ------------------------------------------------- columns + on_load wiring

@pytest.mark.asyncio
async def test_last_card_columns_are_added_to_a_pre_0_57_schema():
    from sqlalchemy import inspect, text

    from plugin_playbooks import _ensure_columns

    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        cols = [
            "last_card_action", "last_card_version", "last_card_approval_id",
            "last_card_raised_at", "last_card_decision", "last_card_decided_at",
        ]
        async with engine.begin() as conn:
            for c in cols:
                await conn.execute(text(f"ALTER TABLE playbooks DROP COLUMN {c}"))

        def _cols(sync_conn):
            return [c["name"] for c in inspect(sync_conn).get_columns("playbooks")]

        async with engine.connect() as conn:
            before = await conn.run_sync(_cols)
        assert not set(cols) & set(before)
        await _ensure_columns(engine)
        async with engine.connect() as conn:
            after = await conn.run_sync(_cols)
        assert set(cols) <= set(after)
        await _ensure_columns(engine)  # idempotent
        async with engine.connect() as conn:
            assert await conn.run_sync(_cols) == after
        # the guard round-trips through the migrated columns
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as s:
            s.add(Playbook(name="greeter", display_name="greeter",
                           definition={"name": "greeter", "steps": []}))
            await s.commit()
        await publish_guard.remember_card(
            sf, name="greeter", action="publish", version=2, approval_id="card-1",
        )
        assert await publish_guard.note_decision(sf, approval_id="card-1", decision="approved")
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
        assert (pb.last_card_version, pb.last_card_decision) == (2, "approved")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_on_load_subscription_records_the_owner_decision():
    from plugin_playbooks import PlaybooksPlugin

    class _SubBus(_Bus):
        def __init__(self):
            super().__init__()
            self.handlers: dict[str, list] = {}
            self.unsubscribed: list[str] = []

        def subscribe(self, name, handler, background=False):
            self.handlers.setdefault(name, []).append(handler)
            return lambda: self.unsubscribed.append(name)

    class _LoadCtx:
        def __init__(self, sf, bus):
            self.db_session_factory = sf
            self.events = bus

    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sf = async_sessionmaker(engine, expire_on_commit=False)
        bus = _SubBus()
        plugin = PlaybooksPlugin()
        plugin._start_publish_guard(_LoadCtx(sf, bus))
        plugin._start_publish_guard(_LoadCtx(sf, bus))  # idempotent
        assert len(bus.handlers["approval.decided"]) == 1
        assert len(bus.handlers["approval.orphan_decided"]) == 1

        async with sf() as s:
            s.add(Playbook(name="greeter", display_name="greeter",
                           definition={"name": "greeter", "steps": []}))
            await s.commit()
        await publish_guard.remember_card(
            sf, name="greeter", action="publish", version=2, approval_id="card-9",
        )
        handler = bus.handlers["approval.decided"][0]
        await handler({"id": "card-9", "decision": "approved", "reason": None,
                       "decided_by": "owner"})
        await handler("not-a-dict")  # ignored, never raises
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
        assert pb.last_card_decision == "approved"
        assert pb.last_card_decided_at is not None

        await plugin.on_unload()
        assert bus.unsubscribed == ["approval.decided", "approval.orphan_decided"]
        assert plugin._unsub_publish_guard is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approved_orphan_commits_exact_candidate_and_reports_verified_store():
    """The decision, rather than a model reissue, performs the gated effect."""
    from plugin_playbooks import PlaybooksPlugin

    payload = {"name": "greeter", "version": 2, "action": "publish"}
    grants = _Grants(hits={json.dumps(payload, sort_keys=True): True})
    approvals = _Approvals(["pending", "approved"], grants=grants)
    engine, sf, tools, _ = await _env(approvals)
    notices = []

    async def send(title, content, **kwargs):
        notices.append((title, content, kwargs))
        return {"responded": True}

    plugin = PlaybooksPlugin()
    plugin._session_factory = sf
    plugin._ctx = SimpleNamespace(
        tool_registry=SimpleNamespace(
            get=lambda name: SimpleNamespace(handler=tools[name]),
        ),
        send_muted_message=send,
    )
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)
        card = first["approval_id"]
        approvals.statuses[card] = "approved"
        await publish_guard.note_decision(sf, approval_id=card, decision="approved")
        event = {
            "id": card, "kind": "playbook_change", "payload": payload,
            "decision": "approved", "conversation_id": str(OPS),
        }

        await plugin._on_publish_decision({**event, "id": "wrong-card"})
        await plugin._on_publish_decision({**event, "payload": {**payload, "version": 3}})
        assert (await _pb(sf)).live_version == 1
        assert len(approvals.requests) == 1

        await plugin._on_publish_decision(event)
        await __import__("asyncio").gather(*plugin._publish_wakes)
        pb = await _pb(sf)
        assert pb.live_version == 2 and pb.candidate_version is None
        assert len(approvals.requests) == 2
        assert "read-back verified live_version=2" in notices[0][1]

        await plugin._on_publish_decision(event)
        assert len(approvals.requests) == 2  # duplicate is inert
    finally:
        await plugin.on_unload()
        await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_orphan_never_invokes_publish_tool():
    from plugin_playbooks import PlaybooksPlugin

    approvals = _Approvals(["pending"])
    engine, sf, tools, _ = await _env(approvals)
    notices = []

    async def send(title, content, **kwargs):
        notices.append(content)

    plugin = PlaybooksPlugin()
    plugin._session_factory = sf
    plugin._ctx = SimpleNamespace(
        tool_registry=SimpleNamespace(
            get=lambda name: (_ for _ in ()).throw(AssertionError("publish called")),
        ),
        send_muted_message=send,
    )
    try:
        await _seed_live_v1(sf, tools)
        await _candidate(sf, tools)
        first = await _publish(tools)
        await publish_guard.note_decision(
            sf, approval_id=first["approval_id"], decision="rejected",
        )
        await plugin._on_publish_decision({
            "id": first["approval_id"], "kind": "playbook_change",
            "payload": {"name": "greeter", "version": 2, "action": "publish"},
            "decision": "rejected", "conversation_id": str(OPS),
        })
        await __import__("asyncio").gather(*plugin._publish_wakes)
        assert (await _pb(sf)).live_version == 1
        assert "NOT verified live" in notices[0]
    finally:
        await plugin.on_unload()
        await engine.dispose()
