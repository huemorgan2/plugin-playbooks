"""plans/017 — archived playbooks stop squatting their names.

`playbook_propose` over an ARCHIVED name takes over the row (id kept, run
history survives, version counter climbs); over a live name it still errors.
`DELETE /playbooks/{name}/purge` hard-deletes an archived playbook + its
version rows (409 if live, 404 if missing).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookVersion

NOW = datetime.now(timezone.utc)
BASE = "/api/p/plugin-playbooks"

CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _Runner:
    _tools = None
    _agent = None


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    tools = {td.name: h for td, h in build_tools(sf, _Bus(), _Runner())}
    yield sf, tools
    await engine.dispose()


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    routes.init_routes(sf, runner=_Runner())
    app = FastAPI()
    app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://luna.test"
    ) as c:
        yield sf, c
    await engine.dispose()


async def _get(sf, name: str = "greeter") -> Playbook:
    async with sf() as s:
        return (await s.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one()


# --- propose over an archived name ---

@pytest.mark.asyncio
async def test_propose_over_archived_name_takes_over_the_row(env):
    sf, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    old = await _get(sf)
    old_id, old_version = old.id, old.version
    async with sf() as s:
        pb = await s.get(Playbook, old_id)
        pb.status = "archived"
        s.add(PlaybookRun(playbook_id=old_id, playbook_version=old_version,
                          status="done", trigger="manual", is_test=False,
                          started_at=NOW, completed_at=NOW))
        await s.commit()

    out = json.loads(await tools["playbook_propose"](name="greeter", code=NEW_CODE))
    assert out.get("status") == "created", out

    pb = await _get(sf)
    assert pb.id == old_id                        # same row — history survives
    assert pb.status == "enabled"
    assert pb.code == NEW_CODE
    assert pb.definition["steps"][0]["args"]["message"] == "{{ inputs.name }}"
    assert pb.version == old_version + 1          # old runs keep their versions
    assert pb.live_version == pb.version
    assert pb.candidate_version is None
    async with sf() as s:
        runs = (await s.execute(select(PlaybookRun))).scalars().all()
    assert len(runs) == 1 and runs[0].playbook_id == old_id


@pytest.mark.asyncio
async def test_propose_over_live_name_still_errors(env):
    sf, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    for status in ("enabled", "disabled"):
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            pb.status = status
            await s.commit()
        out = json.loads(await tools["playbook_propose"](name="greeter", code=NEW_CODE))
        assert out["error"] == "Playbook 'greeter' already exists"
    assert (await _get(sf)).code == CODE          # untouched


# --- purge route ---

def _defn() -> dict:
    return {"name": "greeter", "display_name": "greeter", "description": "says hi",
            "steps": [{"id": "say", "kind": "tool_call", "tool": "send_chat_message",
                       "args": {"message": "hi"}}]}


async def _seed(sf, *, status: str) -> uuid.UUID:
    async with sf() as s:
        pb = Playbook(name="greeter", display_name="greeter", description="says hi",
                      definition=_defn(), version=2, live_version=2, status=status)
        s.add(pb)
        await s.flush()
        for v in (1, 2):
            s.add(PlaybookVersion(playbook_id=pb.id, version=v, definition=_defn(),
                                  author="agent", message=f"v{v}"))
        await s.commit()
        return pb.id


@pytest.mark.asyncio
async def test_purge_deletes_archived_playbook_and_versions(client):
    sf, c = client
    await _seed(sf, status="archived")
    r = await c.delete(f"{BASE}/playbooks/greeter/purge")
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "greeter", "status": "purged"}
    async with sf() as s:
        assert (await s.execute(select(Playbook))).scalars().all() == []
        assert (await s.execute(select(PlaybookVersion))).scalars().all() == []


@pytest.mark.asyncio
async def test_purge_of_live_playbook_is_409(client):
    sf, c = client
    await _seed(sf, status="enabled")
    r = await c.delete(f"{BASE}/playbooks/greeter/purge")
    assert r.status_code == 409, r.text
    assert "archive" in r.json()["detail"].lower()
    async with sf() as s:                          # nothing deleted
        assert len((await s.execute(select(PlaybookVersion))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_purge_of_missing_playbook_is_404(client):
    _, c = client
    r = await c.delete(f"{BASE}/playbooks/nope/purge")
    assert r.status_code == 404
