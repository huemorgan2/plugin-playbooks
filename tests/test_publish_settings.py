"""0.28.0 (plans/016 phase 6, reshaped by 021) — owner-switchable AGENT gate.

`publish_require_run` (default on) decides whether the test-run gate REFUSES
the AGENT's publish. Off = the gate still runs and is reported, but never
blocks. 021: the owner's own UI promote/rollback NEVER blocks on it — the
click is the consent; only static validation and probes still 422.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from readstage import parse_read_stage
from evidence import EXPLANATION
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook
from plugin_playbooks.runner import PlaybookRunner

BASE = "/api/p/plugin-playbooks"

CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def subscribe(self, name, handler, background: bool = False):
        return lambda: None


class _Tool:
    def __init__(self, handler) -> None:
        self.handler = handler


class _Tools:
    def __init__(self, **tools) -> None:
        self._tools = tools

    def get(self, name: str):
        return self._tools[name]

    def names(self):
        return list(self._tools)


async def _noop(**_kw):
    return {"ok": True}


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(send_chat_message=_Tool(_noop)),
        events=_Bus(),
    )
    handlers = {td.name: h for td, h in build_tools(sf, _Bus(), runner)}
    routes.init_routes(sf, runner=runner)
    app = FastAPI()
    app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://luna.test"
    ) as client:
        yield sf, handlers, client
    await engine.dispose()


async def _seed(handlers) -> None:
    """Live v1 via the agent's propose tool."""
    out = json.loads(await handlers["playbook_propose"](name="greeter", code=CODE))
    assert "error" not in out, out


async def _owner_edit(sf, client) -> None:
    """Owner PUT of the live definition → mints v2 (live), v1 restorable."""
    p = await _pb(sf)
    r = await client.put(f"{BASE}/playbooks/greeter", json={
        "definition": p.definition,
        "message": "edit",
    })
    assert r.status_code == 200, r.text


async def _pb(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


# --- settings surface ---------------------------------------------------------

async def test_defaults_on_and_patch_route(env):
    sf, handlers, client = env
    await _seed(handlers)
    r = await client.get(f"{BASE}/playbooks/greeter")
    assert r.json()["publish_require_run"] is True

    r = await client.patch(f"{BASE}/playbooks/greeter/publish-settings", json={})
    assert r.status_code == 400

    r = await client.patch(
        f"{BASE}/playbooks/greeter/publish-settings", json={"require_run": False},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "greeter"
    assert r.json()["publish_require_run"] is False
    r = await client.get(f"{BASE}/playbooks/greeter")
    assert r.json()["publish_require_run"] is False

    r = await client.patch(f"{BASE}/playbooks/nope/publish-settings", json={"require_run": False})
    assert r.status_code == 404


async def test_tool_sets_the_flags(env):
    sf, handlers, client = env
    await _seed(handlers)
    out = json.loads(await handlers["playbook_set_autonomy"](name="greeter", require_run=False))
    assert out["status"] == "updated"
    assert out["publish_require_run"] is False
    p = await _pb(sf)
    assert p.publish_require_run is False
    out = json.loads(await handlers["playbook_set_autonomy"](name="greeter"))
    assert "Nothing to change" in out["error"]


# --- run gate ----------------------------------------------------------------

async def test_restore_without_run_never_blocks_the_owner(env):
    # 021: no test run of the target version — the owner's promote still
    # goes through regardless of require_run.
    sf, handlers, client = env
    await _seed(handlers)
    await _owner_edit(sf, client)
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 200, r.text
    assert (await _pb(sf)).live_version == 1


async def test_rollback_never_blocks_the_owner(env):
    # 021: no run evidence, flag on — the owner's rollback still goes through.
    sf, handlers, client = env
    await _seed(handlers)
    await _owner_edit(sf, client)
    r = await client.post(f"{BASE}/playbooks/greeter/rollback")
    assert r.status_code == 200, r.text
    assert (await _pb(sf)).live_version == 1


async def test_candidate_tool_publish_run_gate_off(env):
    sf, handlers, client = env
    await _seed(handlers)
    await client.patch(f"{BASE}/playbooks/greeter/publish-settings", json={"require_run": False})
    ticket = parse_read_stage(await handlers["playbook_edit"](name="greeter"))["ticket"]
    out = json.loads(await handlers["playbook_edit"](
        name="greeter", ticket=ticket, code=CODE.replace("says hi", "says hello"),
    ))
    assert "error" not in out, out
    assert (await _pb(sf)).candidate_version == 2
    out = json.loads(await handlers["playbook_publish"](explanation=EXPLANATION, name="greeter"))
    assert out.get("status") == "published", out
    run_gate = next(g for g in out["gates"] if g["gate"] == "test_run")
    assert run_gate["ok"] is False and run_gate["enforced"] is False
    assert (await _pb(sf)).live_version == 2
