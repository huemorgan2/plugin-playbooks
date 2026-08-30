"""0.28.0 (plans/016 phase 6) — owner-switchable publish gates.

`publish_require_specs` / `publish_require_run` (default on) decide whether
the specs gate and the test-run gate REFUSE. Off = the gate still runs and is
reported, but never blocks. Static validation and probes are not switchable.
"""

from __future__ import annotations

import json

import yaml
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookSpec
from plugin_playbooks.runner import PlaybookRunner

from test_versioned_specs import (  # noqa: E402
    CODE, SPEC_FAILING, SPEC_OK, _Bus, _Tool, _Tools, _noop,
)

BASE = "/api/p/plugin-playbooks"
NOW = datetime.now(timezone.utc)


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


async def _seed(handlers, *, specs: dict[str, str] | None = None) -> None:
    """Live v1 via the agent's propose tool, plus the given specs on v1."""
    out = json.loads(await handlers["playbook_propose"](name="greeter", code=CODE))
    assert "error" not in out, out
    for name, spec in (specs or {}).items():
        out = json.loads(await handlers["playbook_spec_add"](
            name="greeter", spec_name=name, spec_yaml=spec,
        ))
        assert "error" not in out, out


async def _owner_edit(sf, client) -> None:
    """Owner PUT of the live definition → mints v2 (live), v1 restorable."""
    p = await _pb(sf)
    r = await client.put(f"{BASE}/playbooks/greeter", json={
        "definition_yaml": yaml.safe_dump(p.definition, sort_keys=False),
        "message": "edit",
    })
    assert r.status_code == 200, r.text


async def _pb(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


async def _green_run(sf, version: int) -> None:
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
        started = NOW - timedelta(minutes=5)
        s.add(PlaybookRun(
            playbook_id=pb.id, playbook_version=version, status="done",
            trigger="manual", is_test=True, started_at=started,
            completed_at=started + timedelta(seconds=1),
        ))
        await s.commit()


# --- settings surface ---------------------------------------------------------

async def test_defaults_on_and_patch_route(env):
    sf, handlers, client = env
    await _seed(handlers)
    r = await client.get(f"{BASE}/playbooks/greeter")
    assert r.json()["publish_require_specs"] is True
    assert r.json()["publish_require_run"] is True

    r = await client.patch(f"{BASE}/playbooks/greeter/publish-settings", json={})
    assert r.status_code == 400

    r = await client.patch(
        f"{BASE}/playbooks/greeter/publish-settings", json={"require_specs": False},
    )
    assert r.status_code == 200
    assert r.json() == {
        "name": "greeter", "publish_require_specs": False, "publish_require_run": True,
    }
    r = await client.get(f"{BASE}/playbooks/greeter")
    assert r.json()["publish_require_specs"] is False
    assert r.json()["publish_require_run"] is True

    r = await client.patch(f"{BASE}/playbooks/nope/publish-settings", json={"require_run": False})
    assert r.status_code == 404


async def test_tool_sets_the_flags(env):
    sf, handlers, client = env
    await _seed(handlers)
    out = json.loads(await handlers["playbook_set_autonomy"](name="greeter", require_run=False))
    assert out["status"] == "updated"
    assert out["publish_require_run"] is False
    assert out["publish_require_specs"] is True
    p = await _pb(sf)
    assert p.publish_require_run is False and p.publish_require_specs is True
    out = json.loads(await handlers["playbook_set_autonomy"](name="greeter"))
    assert "Nothing to change" in out["error"]


# --- specs gate --------------------------------------------------------------

async def test_restore_red_specs_refused_when_required_names_setting(env):
    sf, handlers, client = env
    await _seed(handlers, specs={"bad": SPEC_FAILING})
    # v2 via owner PUT so v1 is restorable; v2 inherits the red spec.
    await _owner_edit(sf, client)
    await _green_run(sf, 1)
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["gate"] == "specs"
    assert "Owner can relax this in Settings → Publish." in body["message"]


async def test_restore_red_specs_allowed_when_specs_gate_off(env):
    sf, handlers, client = env
    await _seed(handlers, specs={"bad": SPEC_FAILING})
    await _owner_edit(sf, client)
    await _green_run(sf, 1)
    await client.patch(f"{BASE}/playbooks/greeter/publish-settings", json={"require_specs": False})
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 200, r.text
    p = await _pb(sf)
    assert p.live_version == 1
    # the spec still ran and its (red) result is cached on the row
    async with sf() as s:
        row = (await s.execute(
            select(PlaybookSpec).where(PlaybookSpec.playbook_version == 1)
        )).scalar_one()
    assert row.last_result and row.last_result.get("passed") is False


async def test_tool_publish_reports_unenforced_specs_gate(env):
    sf, handlers, client = env
    await _seed(handlers, specs={"bad": SPEC_FAILING})
    await client.patch(f"{BASE}/playbooks/greeter/publish-settings", json={"require_specs": False})
    await _owner_edit(sf, client)
    await _green_run(sf, 1)
    out = json.loads(await handlers["playbook_publish"](name="greeter", version=1))
    assert out.get("status") == "published", out
    specs_gate = next(g for g in out["gates"] if g["gate"] == "specs")
    assert specs_gate["ok"] is False
    assert specs_gate["enforced"] is False
    assert "Settings → Publish" in specs_gate["note"]


# --- run gate ----------------------------------------------------------------

async def test_restore_without_run_refused_when_required_names_setting(env):
    sf, handlers, client = env
    await _seed(handlers, specs={"ok": SPEC_OK})
    await _owner_edit(sf, client)
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["gate"] == "test_run"
    assert "Owner can relax this in Settings → Publish." in body["error"]


async def test_restore_without_run_allowed_when_run_gate_off(env):
    sf, handlers, client = env
    await _seed(handlers, specs={"ok": SPEC_OK})
    await _owner_edit(sf, client)
    await client.patch(f"{BASE}/playbooks/greeter/publish-settings", json={"require_run": False})
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 200, r.text
    assert (await _pb(sf)).live_version == 1


async def test_rollback_honours_both_flags(env):
    sf, handlers, client = env
    await _seed(handlers, specs={"bad": SPEC_FAILING})
    await _owner_edit(sf, client)
    r = await client.post(f"{BASE}/playbooks/greeter/rollback")
    assert r.status_code == 422
    await client.patch(
        f"{BASE}/playbooks/greeter/publish-settings",
        json={"require_specs": False, "require_run": False},
    )
    r = await client.post(f"{BASE}/playbooks/greeter/rollback")
    assert r.status_code == 200, r.text
    assert (await _pb(sf)).live_version == 1


async def test_candidate_tool_publish_run_gate_off(env):
    sf, handlers, client = env
    await _seed(handlers, specs={"ok": SPEC_OK})
    await client.patch(f"{BASE}/playbooks/greeter/publish-settings", json={"require_run": False})
    ticket = parse_read_stage(await handlers["playbook_edit"](name="greeter"))["ticket"]
    out = json.loads(await handlers["playbook_edit"](
        name="greeter", ticket=ticket, code=CODE.replace("says hi", "says hello"),
    ))
    assert "error" not in out, out
    assert (await _pb(sf)).candidate_version == 2
    out = json.loads(await handlers["playbook_publish"](name="greeter"))
    assert out.get("status") == "published", out
    run_gate = next(g for g in out["gates"] if g["gate"] == "test_run")
    assert run_gate["ok"] is False and run_gate["enforced"] is False
    assert (await _pb(sf)).live_version == 2
