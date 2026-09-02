"""0.28.0 (plans/016 phase 5) — specs travel with versions.

Every version number is minted by `versioning.mint_version`, which copies the
source version's specs onto the new number with a fresh result cache. Reads,
writes and the publish gates all address ONE version's set, so restoring an
old version evaluates that version's own tests — rollback works again under
an "all tests green" gate.
"""

from __future__ import annotations

import json

import yaml
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from readstage import parse_read_stage
from evidence import EXPLANATION
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.__init__ import backfill_spec_versions
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookSpec, PlaybookVersion
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.versioning import copy_specs, mint_version

BASE = "/api/p/plugin-playbooks"
NOW = datetime.now(timezone.utc)

CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
SPEC_OK = (
    "inputs: {greeting: 'hi Roy'}\n"
    "expect:\n"
    "  status: done\n"
    "  tool_calls:\n"
    "    send_chat_message: {count: 1, args_contain: {message: 'Roy'}}\n"
)
SPEC_FAILING = (
    "inputs: {greeting: 'hi Roy'}\n"
    "expect:\n"
    "  tool_calls:\n"
    "    send_chat_message: {args_contain: {message: 'Slartibartfast'}}\n"
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


async def _specs(sf, version: int) -> dict[str, PlaybookSpec]:
    async with sf() as s:
        rows = (await s.execute(
            select(PlaybookSpec).where(PlaybookSpec.playbook_version == version)
        )).scalars().all()
    return {r.name: r for r in rows}


def _yaml(defn: dict) -> str:
    return yaml.safe_dump(defn, sort_keys=False)


async def _pb(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


async def _ticket(handlers) -> str:
    return parse_read_stage(await handlers["playbook_edit"](name="greeter"))["ticket"]


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


# --- minting inherits specs ---------------------------------------------------

@pytest.mark.asyncio
async def test_candidate_save_duplicates_live_specs(env):
    sf, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](name="greeter", spec_name="s1", spec_yaml=SPEC_OK)
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers),
        code=CODE.replace("says hi", "says hello"),
    )
    pb = await _pb(sf)
    assert pb.live_version == 1 and pb.candidate_version == 2
    v1, v2 = await _specs(sf, 1), await _specs(sf, 2)
    assert set(v1) == {"s1"} and set(v2) == {"s1"}
    assert v1["s1"].id != v2["s1"].id
    # the copy ran against the candidate (auto-run), live's cache is its own
    assert v2["s1"].last_version == 2
    assert v1["s1"].last_version == 1


@pytest.mark.asyncio
async def test_mint_resets_the_copied_result_cache(env):
    sf, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](name="greeter", spec_name="s1", spec_yaml=SPEC_OK)
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalar_one()
        await mint_version(
            s, pb, definition=pb.definition, code=pb.code, manifest=pb.manifest,
            author="test", message="m", source_version=1,
        )
        await s.commit()
    v2 = await _specs(sf, 2)
    assert v2["s1"].last_result is None and v2["s1"].last_run_at is None
    assert v2["s1"].last_version is None
    # plans/022 P3: the copy is the original spec plus provenance
    v1_spec = (await _specs(sf, 1))["s1"].spec
    assert v2["s1"].spec == {**v1_spec, "carried_from": 1}
    # idempotent: names already on the target are not duplicated
    async with sf() as s:
        assert await copy_specs(s, pb.id, 1, 2) == 0


@pytest.mark.asyncio
async def test_spec_added_on_candidate_does_not_touch_live(env):
    sf, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](name="greeter", spec_name="s1", spec_yaml=SPEC_OK)
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers),
        code=CODE.replace("says hi", "says hello"),
    )
    out = json.loads(await handlers["playbook_spec_add"](
        name="greeter", spec_name="s2", spec_yaml=SPEC_OK,
    ))
    assert out["ran_against_version"] == 2
    assert set(await _specs(sf, 1)) == {"s1"}
    assert set(await _specs(sf, 2)) == {"s1", "s2"}
    listed = json.loads(await handlers["playbook_spec_list"](name="greeter", version="live"))
    assert listed["version"] == 1 and listed["count"] == 1
    # delete on the candidate only (plans/022 P3: s1 is carried — needs why=)
    json.loads(await handlers["playbook_spec_delete"](
        name="greeter", spec_name="s1", why="superseded by s2",
    ))
    assert set(await _specs(sf, 1)) == {"s1"}
    assert set(await _specs(sf, 2)) == {"s2"}


@pytest.mark.asyncio
async def test_owner_put_and_manifest_save_mint_with_specs(env):
    sf, handlers, client = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](name="greeter", spec_name="s1", spec_yaml=SPEC_OK)
    r = await client.put(f"{BASE}/playbooks/greeter/manifest", json={"manifest": "# new"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2
    assert set(await _specs(sf, 2)) == {"s1"}
    pb = await _pb(sf)
    r = await client.put(
        f"{BASE}/playbooks/greeter",
        json={"definition_yaml": _yaml(pb.definition), "message": "owner edit"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3
    assert set(await _specs(sf, 3)) == {"s1"}
    assert len(await _specs(sf, 1)) == 1  # untouched


# --- reads are per version ----------------------------------------------------

@pytest.mark.asyncio
async def test_routes_list_and_run_specs_by_version(env):
    sf, handlers, client = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](name="greeter", spec_name="s1", spec_yaml=SPEC_OK)
    # candidate breaks the expectation; its copy of s1 goes red, live stays green
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers),
        code=CODE.replace("inputs.greeting", "'plain hello'"),
    )
    r = await client.get(f"{BASE}/playbooks/greeter/specs")
    assert r.json()["version"] == 2                       # default: candidate
    assert r.json()["specs"][0]["last_result"]["passed"] is False
    r = await client.get(f"{BASE}/playbooks/greeter/specs?version=1")
    assert r.json()["version"] == 1
    assert r.json()["specs"][0]["last_result"]["passed"] is True
    r = await client.post(f"{BASE}/playbooks/greeter/specs/run?version=1")
    assert r.status_code == 200 and r.json()["ran_against_version"] == 1
    assert r.json()["failed"] == 0
    r = await client.post(f"{BASE}/playbooks/greeter/specs/run?version=2")
    assert r.json()["failed"] == 1
    r = await client.get(f"{BASE}/playbooks/greeter/versions")
    by_v = {row["version"]: row["specs"] for row in r.json()}
    assert by_v[1] == {"total": 1, "failed": 0, "green": 1}
    assert by_v[2] == {"total": 1, "failed": 1, "green": 0}


# --- the gates read the target version's specs --------------------------------

@pytest.mark.asyncio
async def test_restore_runs_the_restored_versions_own_specs(env):
    sf, handlers, client = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](name="greeter", spec_name="s1", spec_yaml=SPEC_OK)
    # v2 = version whose copy of s1 is red: mint v2 as live by an owner PUT
    # of a definition that breaks s1, then add a v2-only spec.
    pb = await _pb(sf)
    broken = dict(pb.definition)
    broken["steps"] = [{"id": "say", "kind": "tool_call", "tool": "send_chat_message",
                        "args": {"message": "plain hello"}}]
    r = await client.put(f"{BASE}/playbooks/greeter", json={"definition_yaml": _yaml(broken)})
    assert r.status_code == 200 and r.json()["version"] == 2
    await handlers["playbook_spec_add"](name="greeter", spec_name="only-v2", spec_yaml=SPEC_FAILING)
    assert set(await _specs(sf, 2)) == {"s1", "only-v2"}
    assert set(await _specs(sf, 1)) == {"s1"}

    await _green_run(sf, 1)
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 1})
    assert r.status_code == 200, r.text            # v1's own spec is green
    assert (await _pb(sf)).live_version == 1

    # and the other way: v2's set is red → 021: the owner still goes
    # through, and v2's own specs were the ones refreshed (red cached).
    await _green_run(sf, 2)
    r = await client.post(f"{BASE}/playbooks/greeter/promote", json={"version": 2})
    assert r.status_code == 200, r.text
    assert (await _pb(sf)).live_version == 2
    async with sf() as s:
        rows = (await s.execute(select(PlaybookSpec).where(
            PlaybookSpec.playbook_version == 2))).scalars().all()
    assert {r_.name for r_ in rows} == {"s1", "only-v2"}
    assert all(r_.last_result and r_.last_result.get("passed") is False for r_ in rows)


@pytest.mark.asyncio
async def test_tool_publish_restore_uses_the_same_gate(env):
    sf, handlers, client = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](name="greeter", spec_name="s1", spec_yaml=SPEC_OK)
    pb = await _pb(sf)
    r = await client.put(f"{BASE}/playbooks/greeter", json={"definition_yaml": _yaml(pb.definition)})
    assert r.json()["version"] == 2
    # make v1's spec red (edit the stored spec directly — the content is fine)
    async with sf() as s:
        row = (await s.execute(select(PlaybookSpec).where(
            PlaybookSpec.playbook_version == 1))).scalar_one()
        row.spec = {**row.spec, "expect": {"tool_calls": {
            "send_chat_message": {"args_contain": {"message": "Slartibartfast"}}}}}
        await s.commit()
    await _green_run(sf, 1)
    out = json.loads(await handlers["playbook_publish"](explanation=EXPLANATION, name="greeter", version=1))
    assert out["gate"] == "specs"
    assert (await _pb(sf)).live_version == 2


@pytest.mark.asyncio
async def test_rollback_reads_the_target_versions_specs(env):
    sf, handlers, client = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](name="greeter", spec_name="s1", spec_yaml=SPEC_OK)
    pb = await _pb(sf)
    r = await client.put(f"{BASE}/playbooks/greeter", json={"definition_yaml": _yaml(pb.definition)})
    async with sf() as s:                              # v2 promoted from v1
        row = (await s.execute(select(PlaybookVersion).where(
            PlaybookVersion.version == 2))).scalar_one()
        row.promoted_from = 1
        await s.commit()
    await _green_run(sf, 1)
    r = await client.post(f"{BASE}/playbooks/greeter/rollback")
    assert r.status_code == 200, r.text
    assert r.json()["live_version"] == 1


# --- load-time backfill -------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_pins_legacy_specs_to_live_and_copies_to_candidate(env):
    sf, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers),
        code=CODE.replace("says hi", "says hello"),
    )
    async with sf() as s:                              # a pre-0.28 row
        pb = (await s.execute(select(Playbook))).scalar_one()
        s.add(PlaybookSpec(playbook_id=pb.id, playbook_version=0, name="legacy",
                           spec={"inputs": {}, "expect": {"status": "done"}}))
        await s.commit()
    assert await backfill_spec_versions(sf) == 2       # pinned + copied
    assert set(await _specs(sf, 0)) == set()
    assert set(await _specs(sf, 1)) == {"legacy"}
    assert set(await _specs(sf, 2)) == {"legacy"}
    assert await backfill_spec_versions(sf) == 0       # idempotent
