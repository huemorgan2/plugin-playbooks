"""0.32.0 (luna-plugins plans/016 phase 1) — playbook change plans.

A plan is one row of owner-readable text. Three tools available everywhere;
ONE enforcement point: publish/rollback (tool) and promote (route) refuse
without a plan in a publishable status. The card leads with the plan;
`plans_full_power` skips the card (audited) but never the plan row;
outcome facts are code-stamped, the narrative comes from plan_finish.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from evidence import EXPLANATION, PLAN_BODY, green_run, make_plan, seed_plan
from readstage import parse_read_stage
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import routes
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import (
    Base,
    Playbook,
    PlaybookPlan,
    PlaybookRun,
    PlaybookVersion,
)
from plugin_playbooks.plans import record_rejection, set_full_power

BASE = "/api/p/plugin-playbooks"
NOW = datetime.now(timezone.utc)
OPS = uuid.uuid4()


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def subscribe(self, name, handler, background: bool = False):
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

    async def request(self, **kw):
        self.requests.append(kw)
        return _Decision(self._decision, self._reason)

    async def record_auto_approval(self, **kw):
        self.autos.append(kw)


class _Ctx:
    def __init__(self, approvals, ops=OPS):
        self.approval = approvals
        self._ops = ops
        self.current_conversation_id = None

    async def ops_conversation_id(self):
        return self._ops


CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")


async def _env(approvals=None):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    ctx = _Ctx(approvals) if approvals is not None else None
    tools = {td.name: h for td, h in build_tools(sf, _Bus(), _StubRunner(), ctx)}
    return engine, sf, tools


async def _green_candidate(sf, tools) -> None:
    await tools["playbook_propose"](name="greeter", code=CODE)
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"], code=NEW_CODE,
    )
    await green_run(sf, 2)


async def _plan_row(sf, plan_id) -> PlaybookPlan:
    async with sf() as s:
        return (await s.execute(
            select(PlaybookPlan).where(PlaybookPlan.id == uuid.UUID(plan_id))
        )).scalar_one()


# --- tool lifecycle ---------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_write_read_finish_lifecycle():
    engine, sf, tools = await _env()
    try:
        out = json.loads(await tools["playbook_plan_write"](
            title="Fix the greeter playbook", body=PLAN_BODY,
            playbook_refs=["greeter"],
        ))
        assert out["status"] == "proposed"
        assert "playbook_publish" in out["note"]
        pid = out["plan_id"]

        listed = json.loads(await tools["playbook_plan_read"]())
        assert listed["count"] == 1
        assert listed["plans"][0]["plan_id"] == pid
        assert listed["plans"][0]["has_execution_summary"] is False

        detail = json.loads(await tools["playbook_plan_read"](plan_id=pid))
        assert detail["plan"]["body"] == PLAN_BODY
        assert detail["plan"]["playbook_refs"] == ["greeter"]

        done = json.loads(await tools["playbook_plan_finish"](
            plan_id=pid,
            summary="The greeter now uses the person's name. Tested with a "
                    "green run and published without issues.",
        ))
        assert done["plan"]["status"] == "done"
        row = await _plan_row(sf, pid)
        assert row.execution_summary and row.status == "done"

        again = json.loads(await tools["playbook_plan_finish"](
            plan_id=pid,
            summary="Trying to write the wrap-up a second time over here.",
        ))
        assert "already done" in again["error"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_plan_write_refuses_thin_body():
    engine, _, tools = await _env()
    try:
        out = json.loads(await tools["playbook_plan_write"](
            title="Fix it", body="short",
        ))
        assert "error" in out
        assert "FOR THE OWNER" in out["hint"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finish_refuses_rejected_plan():
    engine, sf, tools = await _env()
    try:
        pid = await make_plan(tools)
        async with sf() as s:
            await record_rejection(s, pid, "not this week")
        out = json.loads(await tools["playbook_plan_finish"](
            plan_id=pid,
            summary="This summary should be refused because it was rejected.",
        ))
        assert "no execution to summarize" in out["error"]
        assert out["rejection_note"] == "not this week"
    finally:
        await engine.dispose()


# --- the publish gate -------------------------------------------------------

@pytest.mark.asyncio
async def test_publish_without_plan_refused():
    approvals = _Approvals()
    engine, sf, tools = await _env(approvals)
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out["gate"] == "plan_required"
        assert "playbook_plan_write" in out["hint"]
        assert approvals.requests == []
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
        assert pb.live_version == 1 and pb.candidate_version == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_with_unknown_plan_refused():
    approvals = _Approvals()
    engine, sf, tools = await _env(approvals)
    try:
        await _green_candidate(sf, tools)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
            plan_id=str(uuid.uuid4()),
        ))
        assert out["gate"] == "plan_required"
        assert "playbook_plan_read" in out["hint"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_with_plan_stamps_facts_and_steers_to_finish():
    approvals = _Approvals()
    engine, sf, tools = await _env(approvals)
    try:
        await _green_candidate(sf, tools)
        pid = await make_plan(tools)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION, plan_id=pid,
        ))
        assert out["status"] == "published"
        assert out["plan_id"] == pid
        assert "playbook_plan_finish" in out["note"]

        # the card leads with the plan text
        changes = approvals.requests[0]["presentation"]["changes"]
        assert changes[0]["label"] == "Plan"
        assert "Fix the greeter playbook" in changes[0]["text"]
        assert PLAN_BODY in changes[0]["text"]

        row = await _plan_row(sf, pid)
        assert row.status == "approved"          # proposed → approved
        assert len(row.outcome_facts) == 1
        fact = row.outcome_facts[0]
        assert fact["action"] == "publish"
        assert fact["old_live_version"] == 1
        assert fact["new_live_version"] == 2
        assert fact["evidence_run_id"]
        assert fact["at"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owner_rejection_marks_plan_rejected():
    approvals = _Approvals(decision="rejected", reason="not this week")
    engine, sf, tools = await _env(approvals)
    try:
        await _green_candidate(sf, tools)
        pid = await make_plan(tools)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION, plan_id=pid,
        ))
        assert "did not approve" in out["error"]
        row = await _plan_row(sf, pid)
        assert row.status == "rejected"
        assert row.rejection_note == "not this week"

        # a rejected plan cannot carry another publish
        again = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION, plan_id=pid,
        ))
        assert again["gate"] == "plan_required"
        assert again["rejection_note"] == "not this week"
        assert "rejection note" in again["hint"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_full_power_skips_card_but_still_requires_plan():
    approvals = _Approvals()
    engine, sf, tools = await _env(approvals)
    try:
        await _green_candidate(sf, tools)
        async with sf() as s:
            await set_full_power(s, True)

        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out["gate"] == "plan_required"    # full power ≠ no plan

        pid = await make_plan(tools)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION, plan_id=pid,
        ))
        assert out["status"] == "published"
        assert approvals.requests == []          # no blocking card
        assert len(approvals.autos) == 1
        assert "plans_full_power" in approvals.autos[0]["decision_reason"]
        assert approvals.autos[0]["presentation"]["changes"][0]["label"] == "Plan"
        row = await _plan_row(sf, pid)
        assert row.status == "approved" and len(row.outcome_facts) == 1
    finally:
        await engine.dispose()


# --- routes: promote gate, settings, plans ----------------------------------

def _defn(n: int) -> dict:
    return {
        "name": "greeter", "display_name": f"greeter v{n}",
        "description": f"says hi v{n}", "steps": [
            {"id": "say", "kind": "tool_call", "tool": "send_chat_message",
             "args": {"message": f"hi {n}"}},
        ],
    }


@pytest.fixture
async def route_env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    routes.init_routes(sf, runner=_StubRunner())
    app = FastAPI()
    app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
    app.include_router(routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://luna.test"
    ) as client:
        yield sf, client
    await engine.dispose()


async def _seed_restorable(sf) -> None:
    """v2 live, v1 in history with a green live run (restore evidence)."""
    async with sf() as s:
        pb = Playbook(
            name="greeter", display_name="greeter", description="says hi",
            definition=_defn(2), version=2, live_version=2, status="enabled",
        )
        s.add(pb)
        await s.flush()
        started = NOW - timedelta(days=3)
        s.add(PlaybookRun(
            playbook_id=pb.id, playbook_version=1, status="done",
            trigger="schedule", is_test=False, started_at=started,
            completed_at=started + timedelta(seconds=5),
        ))
        for n in (1, 2):
            s.add(PlaybookVersion(
                playbook_id=pb.id, version=n, definition=_defn(n),
                author="agent", message="edit",
                created_at=NOW - timedelta(days=1),
            ))
        await s.commit()


@pytest.mark.asyncio
async def test_promote_route_requires_a_plan(route_env):
    sf, client = route_env
    await _seed_restorable(sf)

    r = await client.post(
        f"{BASE}/playbooks/greeter/promote", json={"version": 1},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["gate"] == "plan_required"
    assert "Plans tab" in detail["message"]

    r = await client.post(
        f"{BASE}/playbooks/greeter/promote",
        json={"version": 1, "plan_id": str(uuid.uuid4())},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["gate"] == "plan_required"

    pid = await seed_plan(sf)
    r = await client.post(
        f"{BASE}/playbooks/greeter/promote",
        json={"version": 1, "plan_id": pid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live_version"] == 1 and body["plan_id"] == pid

    async with sf() as s:
        row = (await s.execute(
            select(PlaybookPlan).where(PlaybookPlan.id == uuid.UUID(pid))
        )).scalar_one()
    assert row.status == "approved"
    assert row.outcome_facts[0]["action"] == "restore"
    assert row.outcome_facts[0]["actor"] == "owner"


async def _seed_candidate(sf) -> None:
    """v1 live, v2 pending candidate, NO runs of v2 (test_run gate red)."""
    async with sf() as s:
        pb = Playbook(
            name="greeter", display_name="greeter", description="says hi",
            definition=_defn(1), version=2, live_version=1,
            candidate_version=2, status="enabled",
        )
        s.add(pb)
        await s.flush()
        for n in (1, 2):
            s.add(PlaybookVersion(
                playbook_id=pb.id, version=n, definition=_defn(n),
                author="agent", message="edit",
                created_at=NOW - timedelta(days=1),
            ))
        await s.commit()


@pytest.mark.asyncio
async def test_promote_candidate_test_run_refusal_names_simulations(route_env):
    sf, client = route_env
    await _seed_candidate(sf)
    pid = await seed_plan(sf)

    r = await client.post(
        f"{BASE}/playbooks/greeter/promote", json={"plan_id": pid},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["gate"] == "test_run"
    assert "simulations" in detail["error"]
    assert "playbook_run_candidate" in detail["hint"]


@pytest.mark.asyncio
async def test_promote_anyway_skips_only_the_test_run_gate(route_env):
    sf, client = route_env
    await _seed_candidate(sf)

    # force does NOT skip the plan gate
    r = await client.post(
        f"{BASE}/playbooks/greeter/promote", json={"force_test_run": True},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["gate"] == "plan_required"

    pid = await seed_plan(sf)
    r = await client.post(
        f"{BASE}/playbooks/greeter/promote",
        json={"plan_id": pid, "force_test_run": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["live_version"] == 2

    async with sf() as s:
        row = (await s.execute(
            select(PlaybookPlan).where(PlaybookPlan.id == uuid.UUID(pid))
        )).scalar_one()
    fact = row.outcome_facts[0]
    assert fact["action"] == "promote"
    assert fact["test_run_forced"] is True
    assert fact["evidence_run_id"] is None


@pytest.mark.asyncio
async def test_promote_anyway_still_refuses_invalid_candidate(route_env):
    sf, client = route_env
    await _seed_candidate(sf)
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
        cand = (await s.execute(
            select(PlaybookVersion).where(
                PlaybookVersion.playbook_id == pb.id,
                PlaybookVersion.version == 2,
            )
        )).scalar_one()
        bad = dict(cand.definition)
        bad["steps"] = [{"id": "say", "kind": "tool_call"}]  # no tool
        cand.definition = bad
        await s.commit()

    pid = await seed_plan(sf)
    r = await client.post(
        f"{BASE}/playbooks/greeter/promote",
        json={"plan_id": pid, "force_test_run": True},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["gate"] == "static_validation"


@pytest.mark.asyncio
async def test_settings_routes_toggle_full_power(route_env):
    _, client = route_env
    r = await client.get(f"{BASE}/playbooks-settings")
    assert r.status_code == 200 and r.json() == {"plans_full_power": False}

    r = await client.patch(
        f"{BASE}/playbooks-settings", json={"plans_full_power": True},
    )
    assert r.json() == {"plans_full_power": True}
    r = await client.get(f"{BASE}/playbooks-settings")
    assert r.json() == {"plans_full_power": True}

    r = await client.patch(
        f"{BASE}/playbooks-settings", json={"plans_full_power": False},
    )
    assert r.json() == {"plans_full_power": False}


@pytest.mark.asyncio
async def test_plans_routes_list_and_detail(route_env):
    sf, client = route_env
    pid = await seed_plan(sf, title="Fix the greeter playbook")

    r = await client.get(f"{BASE}/plans")
    assert r.status_code == 200
    plans = r.json()["plans"]
    assert len(plans) == 1 and plans[0]["plan_id"] == pid

    r = await client.get(f"{BASE}/plans", params={"status": "done"})
    assert r.json()["plans"] == []

    r = await client.get(f"{BASE}/plans/{pid}")
    assert r.status_code == 200
    assert r.json()["body"] == PLAN_BODY

    r = await client.get(f"{BASE}/plans/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_plans_route_filters_by_playbook(route_env):
    # plans/021: the editor's per-playbook Plans tab narrows by ref.
    sf, client = route_env
    pid = await seed_plan(sf, title="Fix the greeter playbook")
    from plugin_playbooks.models import PlaybookPlan
    async with sf() as s:
        s.add(PlaybookPlan(title="Other job", body=PLAN_BODY,
                           playbook_refs=["other-pb"]))
        s.add(PlaybookPlan(title="No refs at all", body=PLAN_BODY,
                           playbook_refs=None))
        await s.commit()

    r = await client.get(f"{BASE}/plans", params={"playbook": "greeter"})
    plans = r.json()["plans"]
    assert [p["plan_id"] for p in plans] == [pid]

    r = await client.get(f"{BASE}/plans", params={"playbook": "nobody"})
    assert r.json()["plans"] == []

    # No param -> unfiltered listing still returns everything.
    r = await client.get(f"{BASE}/plans")
    assert len(r.json()["plans"]) == 3


# --- plans/022: owner plan controls (status PATCH, delete, reopen) ----------

@pytest.mark.asyncio
async def test_plan_status_patch_route(route_env):
    sf, client = route_env
    pid = await seed_plan(sf)

    r = await client.patch(f"{BASE}/plans/{pid}", json={"status": "done"})
    assert r.status_code == 200
    assert r.json()["status"] == "done"

    # the reopen switch: done -> proposed unlocks the plan for the agent
    r = await client.patch(f"{BASE}/plans/{pid}", json={"status": "proposed"})
    assert r.status_code == 200
    assert r.json()["status"] == "proposed"

    r = await client.patch(f"{BASE}/plans/{pid}", json={"status": "bogus"})
    assert r.status_code == 422

    r = await client.patch(
        f"{BASE}/plans/{uuid.uuid4()}", json={"status": "done"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_plan_delete_route(route_env):
    sf, client = route_env
    pid = await seed_plan(sf)

    r = await client.delete(f"{BASE}/plans/{pid}")
    assert r.status_code == 200
    assert r.json() == {"plan_id": pid, "deleted": True}

    r = await client.get(f"{BASE}/plans")
    assert r.json()["plans"] == []

    r = await client.delete(f"{BASE}/plans/{pid}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_owner_reopen_lets_plan_finish_run_again():
    # done plans refuse plan_finish; the owner's status flip is the unlock.
    engine, sf, tools = await _env()
    try:
        out = json.loads(await tools["playbook_plan_write"](
            title="Fix the greeter playbook", body=PLAN_BODY,
        ))
        pid = out["plan_id"]
        await tools["playbook_plan_finish"](
            plan_id=pid,
            summary="Wrapped up: change landed and a green run verified it.",
        )
        again = json.loads(await tools["playbook_plan_finish"](
            plan_id=pid, summary="Second wrap-up attempt over here, refused.",
        ))
        assert "already done" in again["error"]

        # owner reopens from the UI (what PATCH /plans/{id} does)
        async with sf() as s:
            row = (await s.execute(
                select(PlaybookPlan).where(PlaybookPlan.id == uuid.UUID(pid))
            )).scalar_one()
            row.status = "proposed"
            await s.commit()

        redo = json.loads(await tools["playbook_plan_finish"](
            plan_id=pid,
            summary="Reopened by the owner and finished again after rework.",
        ))
        assert redo["plan"]["status"] == "done"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_plan_write_note_reminds_about_the_subagent():
    # plans/022: after writing a plan the agent is pointed at playbook_agent
    # (the authoring sub-agent) as the way to execute the change.
    engine, sf, tools = await _env()
    try:
        out = json.loads(await tools["playbook_plan_write"](
            title="Fix the greeter playbook", body=PLAN_BODY,
        ))
        assert "playbook_agent" in out["note"]
        assert out["plan_id"] in out["note"]
    finally:
        await engine.dispose()
