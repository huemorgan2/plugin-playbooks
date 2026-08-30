"""plans/016 phase 2 — `GET /playbooks/{name}/versions/{n}` and
`GET /playbooks/{name}/runs?version=N` (what the Versions tab reads)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookVersion

NOW = datetime.now(timezone.utc)
BASE = "/api/p/plugin-playbooks"


class _StubRunner:
    _tools = None
    _agent = None


def _defn(n: int) -> dict:
    return {"name": "greeter", "display_name": f"greeter v{n}",
            "description": f"says hi v{n}",
            "steps": [{"id": "say", "kind": "tool_call", "tool": "send_chat_message",
                       "args": {"message": f"hi {n}"}}]}


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    routes.init_routes(sf, runner=_StubRunner())
    app = FastAPI()
    app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://luna.test"
    ) as c:
        yield sf, c
    await engine.dispose()


async def _seed(sf, *, rows: bool) -> None:
    """v2 live (manifest 'M2'), v1 stored with code 'C1' + manifest 'M1',
    two runs of v1 and one of v2. `rows=False` → legacy playbook, no rows."""
    async with sf() as s:
        pb = Playbook(name="greeter", display_name="greeter", description="says hi",
                      definition=_defn(2), manifest="M2", version=2, live_version=2,
                      status="enabled")
        s.add(pb)
        await s.flush()
        for ver, i in ((1, 0), (1, 1), (2, 2)):
            started = NOW - timedelta(days=3, minutes=10 - i)
            s.add(PlaybookRun(playbook_id=pb.id, playbook_version=ver, status="done",
                              trigger="manual", is_test=False, started_at=started,
                              completed_at=started + timedelta(seconds=5)))
        if rows:
            s.add(PlaybookVersion(playbook_id=pb.id, version=1, definition=_defn(1),
                                  code="C1", manifest="M1", author="agent",
                                  message="first", created_at=NOW - timedelta(days=2)))
            s.add(PlaybookVersion(playbook_id=pb.id, version=2, definition=_defn(2),
                                  manifest="M2", author="owner", message="edit",
                                  promoted_from=1, created_at=NOW - timedelta(days=1)))
        await s.commit()


@pytest.mark.asyncio
async def test_get_stored_version(client):
    sf, c = client
    await _seed(sf, rows=True)
    r = await c.get(f"{BASE}/playbooks/greeter/versions/1")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["version"] == 1 and b["live"] is False and b["candidate"] is False
    assert b["definition"]["steps"][0]["args"]["message"] == "hi 1"
    assert b["code"] == "C1" and b["manifest"] == "M1"
    assert b["author"] == "agent" and b["message"] == "first"
    assert b["runs"] == 2                                  # v1's runs only
    r = await c.get(f"{BASE}/playbooks/greeter/versions/2")
    b = r.json()
    assert b["live"] is True and b["promoted_from"] == 1 and b["runs"] == 1


@pytest.mark.asyncio
async def test_get_legacy_live_version_without_a_row(client):
    sf, c = client
    await _seed(sf, rows=False)
    r = await c.get(f"{BASE}/playbooks/greeter/versions/2")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["live"] is True and b["manifest"] == "M2" and b["runs"] == 1
    assert b["definition"]["steps"][0]["args"]["message"] == "hi 2"
    # ...and it matches what list_versions synthesizes
    lst = (await c.get(f"{BASE}/playbooks/greeter/versions")).json()
    assert [v["version"] for v in lst] == [2] and lst[0]["live"] is True


@pytest.mark.asyncio
async def test_get_version_404s(client):
    sf, c = client
    await _seed(sf, rows=False)
    assert (await c.get(f"{BASE}/playbooks/greeter/versions/1")).status_code == 404
    assert (await c.get(f"{BASE}/playbooks/nope/versions/1")).status_code == 404


@pytest.mark.asyncio
async def test_runs_filtered_by_version(client):
    sf, c = client
    await _seed(sf, rows=True)
    assert len((await c.get(f"{BASE}/playbooks/greeter/runs")).json()) == 3
    assert len((await c.get(f"{BASE}/playbooks/greeter/runs?version=1")).json()) == 2
    assert len((await c.get(f"{BASE}/playbooks/greeter/runs?version=2")).json()) == 1
    assert (await c.get(f"{BASE}/playbooks/greeter/runs?version=9")).json() == []
