"""0.30.0 (plans/018 phase 1) — publish is the aggregate, explainable approval.

After every gate passes, the handler raises ONE `playbook_change` approval:
plain-language explanation up front (luna 094 presentation), technical diff
collapsed behind it. The flip happens only after the owner approves, outside
any row lock, and re-checks the approved target is still current.
"""

from __future__ import annotations

import json
import uuid

import pytest

from evidence import EXPLANATION, green_run
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
    def __init__(self, decision="approved", reason=None):
        self.decision = decision
        self.reason = reason


class _Approvals:
    def __init__(self, decision="approved", reason=None):
        self.requests: list[dict] = []
        self.autos: list[dict] = []
        self._decision = decision
        self._reason = reason
        self.on_request = None  # async hook fired before the decision returns

    async def request(self, **kw):
        self.requests.append(kw)
        if self.on_request is not None:
            await self.on_request()
        return _Decision(self._decision, self._reason)

    async def record_auto_approval(self, **kw):
        self.autos.append(kw)


OPS = uuid.uuid4()


class _Ctx:
    def __init__(self, approvals, ops=OPS, state=None):
        self.approval = approvals
        self._ops = ops
        self._state = state

    async def ops_conversation_id(self):
        return self._ops

    def conversation_state(self):
        return self._state


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


async def _live_state(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


@pytest.mark.asyncio
async def test_missing_or_short_explanation_is_a_steering_refusal():
    approvals = _Approvals()
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        for bad in ({}, {"explanation": "fixed it."}):
            out = json.loads(await tools["playbook_publish"](name="greeter", **bad))
            assert "explanation" in out["error"]
            assert "OWNER" in out["hint"]
        pb = await _live_state(sf)
        assert pb.live_version == 1
        assert pb.candidate_version == 2
        assert approvals.requests == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_files_one_rich_approval_then_flips():
    approvals = _Approvals()
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out["status"] == "published"
        assert out["live_version"] == 2

        assert len(approvals.requests) == 1
        req = approvals.requests[0]
        assert req["kind"] == "playbook_change"
        assert req["payload"] == {
            "name": "greeter", "version": 2, "action": "publish",
        }
        assert req["conversation_id"] == OPS
        pres = req["presentation"]
        assert pres["eyebrow"] == "Playbook change"
        assert len(pres["headline"]) <= 90
        assert EXPLANATION in pres["explanation"]
        assert "Evidence:" in pres["explanation"]
        diffs = [c for c in pres["changes"] if c["kind"] == "diff"]
        assert diffs and diffs[0]["label"] == "Playbook code"
        assert "inputs.greeting" in diffs[0]["before"]
        assert "inputs.name" in diffs[0]["after"]
        # presentation stays advisory — never inside the payload
        assert "presentation" not in req["payload"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rejected_publish_leaves_live_untouched():
    approvals = _Approvals(decision="rejected", reason="not this week")
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert "did not approve" in out["error"]
        assert out["owner_reason"] == "not this week"
        assert "stand down" in out["hint"]
        pb = await _live_state(sf)
        assert pb.live_version == 1
        assert pb.candidate_version == 2  # candidate survives for later
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_candidate_changed_during_approval_wait_refuses_flip():
    approvals = _Approvals()
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)

        async def edit_while_pending():
            async with sf() as s:
                pb = (await s.execute(select(Playbook))).scalar_one()
                pb.candidate_version = 99  # a newer save superseded v2
                await s.commit()

        approvals.on_request = edit_while_pending
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert "candidate changed" in out["error"]
        pb = await _live_state(sf)
        assert pb.live_version == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_autonomy_auto_in_fix_publish_records_audit_only():
    approvals = _Approvals()
    engine, sf, tools = await _env(_Ctx(approvals, state="fix_publish"))
    try:
        await _green_candidate(sf, tools)
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            pb.publish_autonomy = "auto"
            await s.commit()
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out["status"] == "published"
        assert approvals.requests == []  # no blocking ask
        assert len(approvals.autos) == 1
        assert approvals.autos[0]["kind"] == "playbook_change"
        assert approvals.autos[0]["presentation"]["eyebrow"] == "Playbook change"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rollback_carries_explanation_and_reverse_diff():
    approvals = _Approvals()
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        await tools["playbook_publish"](name="greeter", explanation=EXPLANATION)
        await green_run(sf, 1)  # restore evidence: v1 has a completed run
        out = json.loads(await tools["playbook_rollback"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out["status"] == "rolled_back"
        req = approvals.requests[-1]
        assert req["payload"]["action"] == "rollback"
        diffs = [c for c in req["presentation"]["changes"] if c["kind"] == "diff"]
        code = [c for c in diffs if c["label"] == "Playbook code"][0]
        assert "inputs.name" in code["before"]
        assert "inputs.greeting" in code["after"]
    finally:
        await engine.dispose()
