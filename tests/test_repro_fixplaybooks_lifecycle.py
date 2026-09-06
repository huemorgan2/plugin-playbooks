"""RED reproduction — fix-playbooks stage 1, lifecycle trust holes
(luna-fixer plans/2026-09-06-playbook-publish-verify and
plans/2026-09-06-manifest-set-live-bypass; bugs-catalog items B1, B2, C).

Production evidence, 2026-09-05 on vaselin-scanny-2:
- the agent claimed "v59 is live" then "v61 is live" while live_version read
  60 — publish success is narrated from intent, never verified;
- the approval wake → re-issue → re-gate loop minted 3 cards for one
  publish;
- playbook_manifest_set silently flipped v60 live over the v59 candidate
  that was at that moment awaiting owner approval.

Assertions state the DESIRED contract, so every test FAILS on current code.
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
        self.request_id = uuid.uuid4()


class _Approvals:
    """Approvals stub answering every request with a fixed decision."""

    def __init__(self, decision="approved"):
        self.requests: list[dict] = []
        self._decision = decision
        # what the OWNER decided on already-raised cards — visible to the
        # approval system, invisible to the plugin (that blindness is the
        # loop-guard defect).
        self.owner_decisions: dict[str, str] = {}

    async def request_nowait(self, **kw):
        self.requests.append(kw)
        return _Decision(self._decision)

    async def request(self, **kw):
        return await self.request_nowait(**kw)

    async def record_auto_approval(self, **kw):
        pass


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
MANIFEST = "## Purpose\nGreets the owner.\n## Never\nNever email anyone.\n"


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
    await tools["playbook_edit"](name="greeter", ticket=read["ticket"], code=NEW_CODE)
    await green_run(sf, 2)


async def _live(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


async def test_publish_success_carries_verified_readback():
    """DESIRED (publish-verify plan §1): a successful publish re-reads the
    stored row and returns machine truth — {"verified": true, live_version
    from the READ, not the intent} — plus a hint pinning what the agent may
    claim. CURRENT: the result echoes the in-memory object it just mutated;
    nothing is verified, and the agent's "vNN is live ✅" narrative is
    unconstrained (two false live-claims in production on 09-05)."""
    approvals = _Approvals()
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out.get("status") == "published"
        assert out.get("verified") is True, (
            "publish result carries no read-back verification — success is "
            "narrated from intent, which is how 'v59 is live' and 'v61 is "
            "live' were both claimed while live_version read 60"
        )
    finally:
        await engine.dispose()


async def test_approved_then_regated_same_payload_trips_loop_guard():
    """DESIRED (publish-verify plan §2): when the owner already APPROVED
    this exact (playbook, action, version) and the re-issued publish
    re-gates anyway (the core grant hole, or any regression), the handler
    must NOT mint another card — it must fail loud ('the approval flow is
    broken; stop'). CURRENT: it mints a fresh card with the same 'you will
    be woken' hint every cycle — 3 cards for one publish on 09-05."""
    approvals = _Approvals(decision="pending")
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        await _green_candidate(sf, tools)

        first = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert first["status"] == "awaiting_owner_approval"
        assert len(approvals.requests) == 1

        # The owner approves card 1; the wake re-issues the exact call, and
        # the approval layer (grant hole) answers "pending" again.
        approvals.owner_decisions[first["approval_id"]] = "approved"
        second = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))

        assert len(approvals.requests) == 1, (
            "re-issue after an approval minted a SECOND card for the same "
            "(playbook, action, version) — this is the unbounded "
            "approve→wake→re-gate loop (3 cards in production)"
        )
        assert second.get("status") != "awaiting_owner_approval", (
            "the handler re-parked on a fresh card instead of failing loud "
            "that the approval flow is broken"
        )
    finally:
        await engine.dispose()


async def test_manifest_set_does_not_flip_live():
    """DESIRED (manifest-set-live-bypass plan): playbook_manifest_set saves
    a CANDIDATE; live_version moves only through publish and its gates.
    CURRENT: it mints a version and sets live_version directly
    (agent_tools.py:2003-2012) — the side door that silently put v60 live
    over the v59 candidate awaiting owner approval."""
    approvals = _Approvals()
    engine, sf, tools = await _env(_Ctx(approvals))
    try:
        # v1 live, v2 is a pending candidate (exactly the 09-05 shape:
        # a candidate awaiting approval when manifest_set fires).
        await tools["playbook_propose"](name="greeter", code=CODE)
        read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
        await tools["playbook_edit"](
            name="greeter", ticket=read["ticket"], code=NEW_CODE,
        )
        pb = await _live(sf)
        assert pb.live_version == 1 and pb.candidate_version == 2

        await tools["playbook_manifest_set"](name="greeter", manifest=MANIFEST)

        pb = await _live(sf)
        assert pb.live_version == 1, (
            f"manifest_set moved live_version to {pb.live_version} with no "
            "candidate, no publish gates, and no approval — the exact "
            "mechanism that silently superseded the v59 candidate in "
            "production"
        )
        assert approvals.requests == [], (
            "no approval was requested for a live change"
        )
    finally:
        await engine.dispose()
