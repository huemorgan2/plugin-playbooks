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


@pytest.mark.asyncio
async def test_runs_carry_their_version(client):
    """plans/016 phase 4: the Versions tab selects a run's version before
    overlaying it, so summaries and detail both say which version ran."""
    sf, c = client
    await _seed(sf, rows=True)
    runs = (await c.get(f"{BASE}/playbooks/greeter/runs")).json()
    assert sorted(r["playbook_version"] for r in runs) == [1, 1, 2]
    detail = (await c.get(f"{BASE}/playbooks/runs/{runs[0]['id']}")).json()
    assert detail["playbook_version"] == runs[0]["playbook_version"]


# --- 0.38.0: duplicate (playbook, version) rows — legacy damage ---------------
# The pre-0.32 edit path snapshotted at the current counter even when a row
# for that number existed, so hosted DBs hold e.g. two v33s. Reads must not
# 500, the load-time heal must delete the redundant row, and the minting
# counter must never re-issue a taken number.

async def _add_duplicate_v1(sf) -> None:
    """A second v1 row: no lineage (promoted_from NULL), newer created_at —
    the shape the old 'before whole-YAML edit' snapshot left behind."""
    from sqlalchemy import select
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
        s.add(PlaybookVersion(playbook_id=pb.id, version=1, definition=_defn(1),
                              manifest="M1-dup", author="agent",
                              message="before whole-YAML edit",
                              created_at=NOW - timedelta(hours=6)))
        await s.commit()


@pytest.mark.asyncio
async def test_duplicate_version_rows_do_not_500(client):
    sf, c = client
    await _seed(sf, rows=True)
    await _add_duplicate_v1(sf)
    r = await c.get(f"{BASE}/playbooks/greeter/versions/1")
    assert r.status_code == 200, r.text
    b = r.json()
    # deterministic pick: the original row (older) wins over the snapshot
    assert b["message"] == "first" and b["manifest"] == "M1"


@pytest.mark.asyncio
async def test_heal_deletes_the_redundant_duplicate(client):
    from sqlalchemy import select
    from plugin_playbooks.versioning import heal_duplicate_version_rows

    sf, c = client
    await _seed(sf, rows=True)
    await _add_duplicate_v1(sf)

    assert await heal_duplicate_version_rows(sf) == 1
    assert await heal_duplicate_version_rows(sf) == 0  # idempotent

    async with sf() as s:
        rows = (await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.version == 1)
        )).scalars().all()
    assert len(rows) == 1
    assert rows[0].message == "first"  # kept the original, dropped the snapshot


@pytest.mark.asyncio
async def test_mint_version_mints_above_stored_rows(client):
    from sqlalchemy import select
    from plugin_playbooks.versioning import mint_version

    sf, _c = client
    await _seed(sf, rows=True)
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
        pb.version = 1  # counter fell behind the stored rows (max row = 2)
        row = await mint_version(
            s, pb, definition=_defn(3), code=None, manifest="M3",
            author="agent", message="edit", source_version=2,
        )
        await s.commit()
    assert row.version == 3  # above max(rows), not counter+1 == 2
