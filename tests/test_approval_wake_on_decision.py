"""0.45.0 (plans/030) — publish approval rides wake-on-decision on new cores.

With `request_nowait` on the engine (luna plans/103), the publish handler
raises the card WITHOUT parking: a pending decision returns an
awaiting_owner_approval JSON telling the agent it will be WOKEN, and nothing
flips live. Inline short-circuits (grant hit) and rejections behave exactly
as before, and engines without `request_nowait` (old cores) keep the parked
`request()` contract.
"""

from __future__ import annotations

import json
import uuid

import pytest

from evidence import green_run
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook


class _Bus:
    async def emit(self, name: str, payload: dict) -> None:
        pass

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


class _NowaitApprovals:
    """New-core engine: request_nowait present; request() must NOT be hit."""

    def __init__(self, decision="pending", reason=None):
        self.nowait_calls: list[dict] = []
        self.request_calls: list[dict] = []
        self._decision = decision
        self._reason = reason

    async def request(self, **kw):
        self.request_calls.append(kw)
        raise AssertionError("new-core path must use request_nowait, not request")

    async def request_nowait(self, **kw):
        self.nowait_calls.append(kw)
        return _Decision(self._decision, self._reason)

    async def record_auto_approval(self, **kw):
        pass


OPS = uuid.uuid4()
CURRENT = uuid.uuid4()


class _Ctx:
    def __init__(self, approvals, ops=OPS, current=None):
        self.approval = approvals
        self._ops = ops
        if current is not None:
            self.current_conversation_id = current

    async def ops_conversation_id(self):
        return self._ops

    def conversation_state(self):
        return None


CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")


async def _env(ctx):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    tools = {td.name: h for td, h in build_tools(sf, _Bus(), _StubRunner(), ctx)}
    return engine, sf, tools


async def _green_candidate(sf, tools) -> None:
    await tools["playbook_propose"](name="greeter", code=CODE)
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"], code=NEW_CODE,
    )
    await green_run(sf, 2)


async def _live(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


@pytest.mark.asyncio
async def test_pending_returns_wake_promise_and_publishes_nothing():
    approvals = _NowaitApprovals(decision="pending")
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](name="greeter"))

        assert out["status"] == "awaiting_owner_approval"
        assert "WOKEN" in out["hint"]
        assert "retry" in out["hint"]  # the do-NOT-retry contract
        assert len(approvals.nowait_calls) == 1
        assert approvals.request_calls == []
        live = await _live(sf)
        assert live.live_version != 2, "a pending decision must not flip live"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inline_approval_still_publishes():
    approvals = _NowaitApprovals(decision="approved")
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](name="greeter"))

        assert "error" not in out
        assert (await _live(sf)).live_version == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_inline_rejection_keeps_old_contract():
    approvals = _NowaitApprovals(decision="rejected", reason="not now")
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](name="greeter"))

        assert out["owner_reason"] == "not now"
        assert (await _live(sf)).live_version != 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_wake_targets_current_conversation_over_ops():
    approvals = _NowaitApprovals(decision="pending")
    engine, sf, tools = await _env(_Ctx(approvals, current=CURRENT))
    try:
        await _green_candidate(sf, tools)
        await tools["playbook_publish"](name="greeter")

        assert approvals.nowait_calls[0]["conversation_id"] == CURRENT
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_old_core_without_nowait_uses_parked_request():
    class _OldApprovals:
        def __init__(self):
            self.request_calls: list[dict] = []

        async def request(self, **kw):
            self.request_calls.append(kw)
            return _Decision("approved")

        async def record_auto_approval(self, **kw):
            pass

    approvals = _OldApprovals()
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](name="greeter"))

        assert "error" not in out
        assert len(approvals.request_calls) == 1
        assert approvals.request_calls[0]["conversation_id"] == OPS
        assert (await _live(sf)).live_version == 2
    finally:
        await engine.dispose()
