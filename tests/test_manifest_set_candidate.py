"""plans/033 (0.57.0; luna-fixer plans/2026-09-06-manifest-set-live-bypass)
— playbook_manifest_set saves a CANDIDATE, never flips live.

- live_version is unchanged by manifest_set; a candidate row carries the
  manifest; publish of that candidate raises the owner card
  (_request_publish_decision) and applies the manifest on the flip;
- a pending code candidate by the same author + manifest_set → ONE merged
  candidate (operator decision P4-5) carrying both changes;
- a foreign author's candidate → refusal, nothing saved;
- the owner REST PUT /playbooks/{name}/manifest stamps a pending candidate
  row too, so publishing it later cannot revert the owner's edit.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from evidence import EXPLANATION, green_run
from fastapi import FastAPI
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import routes
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.delegation import _delegation_id
from plugin_playbooks.models import Base, Playbook, PlaybookVersion
from plugin_playbooks.versioning import live_version_of


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
    def __init__(self, decision="approved", reason=None):
        self.decision = decision
        self.reason = reason
        self.request_id = uuid.uuid4()


class _Approvals:
    def __init__(self, decision="approved"):
        self.requests: list[dict] = []
        self._decision = decision

    async def request_nowait(self, **kw):
        self.requests.append(kw)
        return _Decision(self._decision)

    async def request(self, **kw):
        return await self.request_nowait(**kw)

    async def record_auto_approval(self, **kw):
        pass


class _Ctx:
    def __init__(self, approvals):
        self.approval = approvals

    async def ops_conversation_id(self):
        return uuid.uuid4()

    def conversation_state(self):
        return None


CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")
MANIFEST = "## Purpose\nGreets the owner.\n## Never\nNever email anyone.\n"
MANIFEST_2 = "## Purpose\nGreets the owner by name.\n"


async def _env(approvals=None):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    approvals = approvals or _Approvals()
    bus = _Bus()
    tools = {
        td.name: h
        for td, h in build_tools(sf, bus, _StubRunner(), _Ctx(approvals))
    }
    return engine, sf, tools, approvals, bus


async def _pb(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


async def _rows(sf) -> dict[int, PlaybookVersion]:
    async with sf() as s:
        return {
            v.version: v
            for v in (await s.execute(select(PlaybookVersion))).scalars().all()
        }


async def _live_v1(sf, tools) -> None:
    await tools["playbook_propose"](name="greeter", code=CODE, manifest=MANIFEST)
    await green_run(sf, 1)
    out = json.loads(await tools["playbook_publish"](
        name="greeter", explanation=EXPLANATION,
    ))
    assert out.get("status") == "published", out


async def _edit(tools, code: str) -> dict:
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    return json.loads(await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"], code=code,
    ))


@pytest.mark.asyncio
async def test_manifest_set_saves_candidate_and_leaves_live_alone():
    engine, sf, tools, approvals, bus = await _env()
    try:
        await _live_v1(sf, tools)
        approvals.requests.clear()
        bus.events.clear()

        out = json.loads(await tools["playbook_manifest_set"](
            name="greeter", manifest=MANIFEST_2, why="intent drifted",
        ))

        assert out["status"] == "manifest_candidate_saved"
        assert out["candidate_version"] == 2 and out["version"] == 2
        assert out["live_version"] == 1
        assert out["note"] == "manifest saved as candidate v2 — publish to go live"
        assert "playbook_publish" in out["next"]
        pb = await _pb(sf)
        assert live_version_of(pb) == 1
        assert pb.candidate_version == 2
        assert pb.manifest == MANIFEST          # live manifest untouched
        rows = await _rows(sf)
        assert rows[2].manifest == MANIFEST_2
        assert rows[2].code == CODE             # live content + new manifest
        assert rows[2].author == "agent"
        assert rows[2].message == "manifest updated: intent drifted"
        assert approvals.requests == []         # no card for a save
        assert ("playbook.candidate.saved", {"name": "greeter", "candidate_version": 2}) in bus.events
        assert not any(n == "playbook.saved" for n, _ in bus.events)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manifest_candidate_publishes_through_the_owner_card():
    engine, sf, tools, approvals, _ = await _env()
    try:
        await _live_v1(sf, tools)
        approvals.requests.clear()
        await tools["playbook_manifest_set"](name="greeter", manifest=MANIFEST_2)

        # the normal gates: a green run of the exact candidate version
        await green_run(sf, 2)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))

        assert out.get("status") == "published", out
        assert out["live_version"] == 2
        assert len(approvals.requests) == 1
        card = approvals.requests[0]
        assert card["kind"] == "playbook_change"
        assert card["payload"] == {"name": "greeter", "version": 2, "action": "publish"}
        labels = [c["label"] for c in card["presentation"]["changes"]]
        assert "Manifest" in labels and "Playbook code" not in labels
        pb = await _pb(sf)
        assert live_version_of(pb) == 2
        assert pb.manifest == MANIFEST_2        # applied on the flip
        assert pb.candidate_version is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manifest_candidate_pending_decision_flips_nothing():
    engine, sf, tools, approvals, _ = await _env(_Approvals(decision="pending"))
    try:
        # v1 live without a card (the stub answers pending)
        await tools["playbook_propose"](name="greeter", code=CODE, manifest=MANIFEST)
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            pb.live_version, pb.candidate_version = 1, None
            await s.commit()
        await tools["playbook_manifest_set"](name="greeter", manifest=MANIFEST_2)
        await green_run(sf, 2)

        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))

        assert out["status"] == "awaiting_owner_approval"
        pb = await _pb(sf)
        assert live_version_of(pb) == 1 and pb.manifest == MANIFEST
        assert pb.candidate_version == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pending_code_candidate_is_merged_with_the_manifest():
    engine, sf, tools, approvals, _ = await _env()
    try:
        await _live_v1(sf, tools)
        edit = await _edit(tools, NEW_CODE)
        assert edit["candidate_version"] == 2

        out = json.loads(await tools["playbook_manifest_set"](
            name="greeter", manifest=MANIFEST_2,
        ))

        assert out["candidate_version"] == 3
        pb = await _pb(sf)
        assert live_version_of(pb) == 1
        assert pb.candidate_version == 3        # one candidate, not two
        rows = await _rows(sf)
        assert rows[3].code == NEW_CODE and rows[3].manifest == MANIFEST_2
        assert rows[3].message == "manifest updated on candidate"
        assert rows[2].code == NEW_CODE and rows[2].manifest == MANIFEST  # history

        # publishing the merged candidate puts BOTH changes live
        approvals.requests.clear()
        await green_run(sf, 3)
        pub = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert pub.get("status") == "published", pub
        labels = [c["label"] for c in approvals.requests[0]["presentation"]["changes"]]
        assert "Manifest" in labels and "Playbook code" in labels
        pb = await _pb(sf)
        assert live_version_of(pb) == 3
        assert pb.code == NEW_CODE and pb.manifest == MANIFEST_2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_manifest_set_refuses_a_foreign_candidate():
    engine, sf, tools, approvals, _ = await _env()
    try:
        await _live_v1(sf, tools)
        did = uuid.uuid4()
        token = _delegation_id.set(did)
        try:
            edit = await _edit(tools, NEW_CODE)
        finally:
            _delegation_id.reset(token)
        assert edit["candidate_version"] == 2

        out = json.loads(await tools["playbook_manifest_set"](
            name="greeter", manifest=MANIFEST_2,
        ))

        assert out["saved"] is False
        assert "never replaced silently" in out["error"]
        assert out["conflict"]["author"] == f"delegation:{did}"
        pb = await _pb(sf)
        assert pb.candidate_version == 2 and pb.version == 2
        assert live_version_of(pb) == 1 and pb.manifest == MANIFEST
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owner_rest_manifest_write_stamps_the_pending_candidate():
    engine, sf, tools, approvals, bus = await _env()
    try:
        await _live_v1(sf, tools)
        await _edit(tools, NEW_CODE)            # candidate v2, manifest M1

        routes.init_routes(sf, runner=_StubRunner(), events=bus)
        app = FastAPI()
        app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
        app.include_router(routes.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://luna.test",
        ) as c:
            r = await c.put(
                "/api/p/plugin-playbooks/playbooks/greeter/manifest",
                json={"manifest": MANIFEST_2},
            )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "manifest_set"

        pb = await _pb(sf)
        assert live_version_of(pb) == 3 and pb.manifest == MANIFEST_2  # owner path stays direct
        assert pb.candidate_version == 2
        rows = await _rows(sf)
        assert rows[2].manifest == MANIFEST_2   # the candidate carries the owner's text

        # publishing the candidate keeps the owner's manifest
        await green_run(sf, 2)
        pub = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert pub.get("status") == "published", pub
        pb = await _pb(sf)
        assert live_version_of(pb) == 2 and pb.manifest == MANIFEST_2
    finally:
        await engine.dispose()
