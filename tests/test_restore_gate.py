"""0.27.1 (plans/016 phase 1) — the restore/rollback test-run gate.

Bug: owner "Promote" on an old version did nothing. The restore gate
required a completed run of that version started AFTER the version row's
`created_at`; snapshot rows are minted at the NEXT edit, so every run the
version ever had predates its own row and the gate always refused (and the
UI swallowed the 422). Version rows are immutable, so for a restore any
completed run of exactly that version is evidence — the `since` bound is
dropped for `include_live=True`. Candidates keep the strict rule.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from evidence import EXPLANATION
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookVersion
from plugin_playbooks import publish

NOW = datetime.now(timezone.utc)
BASE = "/api/p/plugin-playbooks"


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


def _defn(n: int) -> dict:
    return {
        "name": "greeter", "display_name": f"greeter v{n}",
        "description": f"says hi v{n}", "steps": [
            {"id": "say", "kind": "tool_call", "tool": "send_chat_message",
             "args": {"message": f"hi {n}"}},
        ],
    }


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    bus = _Bus()
    runner = _StubRunner()
    tools = {td.name: h for td, h in build_tools(sf, bus, runner)}
    routes.init_routes(sf, runner=runner)
    app = FastAPI()
    app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
    app.include_router(routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://luna.test"
    ) as client:
        yield sf, tools, client
    await engine.dispose()


async def _seed(sf, *, runs_of_v1: list[str]) -> Playbook:
    """v2 live. v1 is a snapshot row minted (created_at) AFTER v1's runs —
    exactly how `_snapshot_version` / `_ensure_live_row` write history."""
    async with sf() as s:
        pb = Playbook(
            name="greeter", display_name="greeter", description="says hi",
            definition=_defn(2), version=2, live_version=2, status="enabled",
        )
        s.add(pb)
        await s.flush()
        for i, status in enumerate(runs_of_v1):
            started = NOW - timedelta(days=3, minutes=10 - i)
            s.add(PlaybookRun(
                playbook_id=pb.id, playbook_version=1, status=status,
                trigger="schedule", is_test=False, started_at=started,
                completed_at=started + timedelta(seconds=5),
            ))
        s.add(PlaybookVersion(
            playbook_id=pb.id, version=1, definition=_defn(1),
            author="agent", message="before whole-YAML edit",
            created_at=NOW - timedelta(days=1),      # after all v1 runs
        ))
        s.add(PlaybookVersion(
            playbook_id=pb.id, version=2, definition=_defn(2),
            author="agent", message="edit", created_at=NOW - timedelta(days=1),
        ))
        await s.commit()
        await s.refresh(pb)
        return pb


async def _live(sf) -> int:
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
        return pb.live_version


# --- the gate itself --------------------------------------------------------

@pytest.mark.asyncio
async def test_restore_accepts_runs_older_than_the_version_row(env):
    sf, _, _ = env
    pb = await _seed(sf, runs_of_v1=["done"])
    async with sf() as s:
        row = (await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.version == 1)
        )).scalar_one()
        gate, refusal, run = await publish.test_run_gate(
            s, pb.id, 1, row.created_at, include_live=True,
        )
    assert refusal is None
    assert gate["ok"] is True and run is not None


@pytest.mark.asyncio
async def test_candidate_gate_still_requires_a_run_after_the_row(env):
    sf, _, _ = env
    pb = await _seed(sf, runs_of_v1=["done"])
    async with sf() as s:
        row = (await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.version == 1)
        )).scalar_one()
        _, refusal, _ = await publish.test_run_gate(
            s, pb.id, 1, row.created_at, include_live=False,
        )
    assert refusal is not None
    assert json.loads(refusal)["gate"] == "test_run"


# --- the REST route the Versions panel calls --------------------------------

@pytest.mark.asyncio
async def test_route_restore_of_an_old_version_goes_live(env):
    sf, _, client = env
    await _seed(sf, runs_of_v1=["failed", "done"])   # latest is green
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["live_version"] == 1 and body["promoted_from"] == 2
    assert await _live(sf) == 1
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
    assert pb.definition["steps"][0]["args"]["message"] == "hi 1"


@pytest.mark.asyncio
async def test_route_restore_never_blocks_when_version_never_ran(env):
    # 021: the owner's click is the consent — no run evidence, still 200;
    # the UI confirm showed the ✗ "never run" bullet first.
    sf, _, client = env
    await _seed(sf, runs_of_v1=[])
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 200, r.text
    assert await _live(sf) == 1


@pytest.mark.asyncio
async def test_route_restore_never_blocks_when_latest_run_failed(env):
    sf, _, client = env
    await _seed(sf, runs_of_v1=["done", "failed"])   # latest is red
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 200, r.text
    assert await _live(sf) == 1


@pytest.mark.asyncio
async def test_route_rollback_uses_the_same_relaxed_gate(env):
    sf, _, client = env
    await _seed(sf, runs_of_v1=["done"])
    async with sf() as s:                              # v2 was promoted from v1
        row = (await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.version == 2)
        )).scalar_one()
        row.promoted_from = 1
        await s.commit()
    r = await client.post(f"{BASE}/playbooks/greeter/rollback")
    assert r.status_code == 200, r.text
    assert r.json()["live_version"] == 1
    assert await _live(sf) == 1


# --- the playbook_publish tool (same gate, same outcome) --------------------

@pytest.mark.asyncio
async def test_tool_restore_matches_the_route(env):
    sf, tools, _ = env
    await _seed(sf, runs_of_v1=["done"])
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", version=1))
    assert out.get("status") == "published", out
    assert await _live(sf) == 1


@pytest.mark.asyncio
async def test_tool_restore_refused_without_any_run(env):
    sf, tools, _ = env
    await _seed(sf, runs_of_v1=[])
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", version=1))
    assert out["gate"] == "test_run"
    assert await _live(sf) == 2
