"""0.10.0 (plans/002 phase 3) — candidate versions, promote, rollback.

A save creates a CANDIDATE version row; live content on the playbook row is
untouched until playbook_publish passes the gate. playbook_rollback restores
the previous live version. dry_run targets the candidate by default;
playbook_run_candidate is a supervised real run of the candidate.
"""

from __future__ import annotations

import json
import uuid

import pytest

from readstage import parse_read_stage
from evidence import EXPLANATION, make_plan
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import backfill_live_version
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookVersion


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _FakeRun:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.status = "done"


class _Runner:
    """Records which playbook OBJECT each entry point received — that is how
    the shim (candidate content) vs the live row is asserted."""

    _tools = None
    _agent = None

    def __init__(self) -> None:
        self.dry_ran: list = []
        self.started: list = []

    async def dry_run(self, playbook, inputs=None):
        self.dry_ran.append(playbook)
        return {"ok": True, "playbook": playbook.name}

    async def start_run_background(
        self, playbook, inputs=None, trigger=None, is_test=False,
    ):
        self.started.append((playbook, trigger))
        self.last_is_test = is_test
        return _FakeRun()

    async def wait_for_run(self, run_id, timeout=None):
        return None


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    bus = _Bus()
    runner = _Runner()
    tools = {td.name: h for td, h in build_tools(sf, bus, runner)}
    yield sf, tools, runner, bus
    await engine.dispose()


CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")
THIRD_CODE = CODE.replace("inputs.greeting", "inputs.nickname")


async def _get(sf, name: str = "greeter") -> Playbook:
    async with sf() as s:
        return (await s.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one()


async def _rows(sf) -> dict[int, PlaybookVersion]:
    async with sf() as s:
        return {
            v.version: v
            for v in (await s.execute(select(PlaybookVersion))).scalars().all()
        }


async def _green_run(sf, version: int, *, is_test: bool = True) -> None:
    """0.26.0: satisfy the publish test-run gate — a completed green run of
    exactly `version`, recorded after the version row was created."""
    from datetime import datetime, timedelta, timezone

    from plugin_playbooks.models import PlaybookRun

    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalars().first()
        s.add(PlaybookRun(
            playbook_id=pb.id, playbook_version=version, status="done",
            trigger="agent-candidate" if is_test else "schedule",
            is_test=is_test, started_at=later,
            completed_at=later + timedelta(seconds=1),
        ))
        await s.commit()


async def _save_candidate(tools, code: str = NEW_CODE) -> dict:
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    return json.loads(await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"], code=code,
    ))


# --- creation + backfill ---

@pytest.mark.asyncio
async def test_propose_goes_live_directly(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    pb = await _get(sf)
    assert pb.version == 1
    assert pb.live_version == 1
    assert pb.candidate_version is None


@pytest.mark.asyncio
async def test_backfill_pins_live_version_on_legacy_rows(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    async with sf() as s:
        await s.execute(update(Playbook).values(live_version=0, version=4))
        await s.commit()
    n = await backfill_live_version(sf)
    assert n == 1
    pb = await _get(sf)
    assert pb.live_version == 4
    assert await backfill_live_version(sf) == 0  # idempotent


# --- candidate save ---

@pytest.mark.asyncio
async def test_save_creates_candidate_and_leaves_live_untouched(env):
    sf, tools, _, bus = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = await _save_candidate(tools)
    assert out["status"] == "candidate_saved"
    assert out["candidate_version"] == 2
    assert out["live_version"] == 1
    assert "playbook_publish" in out["next"]
    pb = await _get(sf)
    assert pb.code == CODE                      # live untouched
    assert pb.definition["steps"][0]["args"] == {"message": "{{ inputs.greeting }}"}
    assert pb.live_version == 1
    assert pb.candidate_version == 2
    rows = await _rows(sf)
    assert rows[1].code == CODE                 # live recorded
    assert rows[2].code == NEW_CODE             # candidate holds new content
    # candidate saves must NOT resync triggers (live didn't change)
    assert not any(n == "playbook.saved" for n, _ in bus.events)
    assert any(n == "playbook.candidate.saved" for n, _ in bus.events)


@pytest.mark.asyncio
async def test_read_stage_returns_candidate_when_one_exists(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await _save_candidate(tools)
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    assert read["editing"] == "candidate"
    assert read["code"] == NEW_CODE
    assert read["live_version"] == 1
    assert read["candidate_version"] == 2


@pytest.mark.asyncio
async def test_second_save_iterates_on_candidate(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await _save_candidate(tools)
    # snippet edit applies to the CANDIDATE code, not live
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"],
        old="inputs.name", new="inputs.nickname",
    ))
    assert out["status"] == "candidate_saved"
    assert out["candidate_version"] == 3
    pb = await _get(sf)
    assert pb.candidate_version == 3            # pointer moved
    rows = await _rows(sf)
    assert rows[2].code == NEW_CODE             # old candidate stays in history
    assert rows[3].code == THIRD_CODE


# --- dry run targeting ---

@pytest.mark.asyncio
async def test_dry_run_defaults_to_candidate(env):
    sf, tools, runner, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await _save_candidate(tools)
    out = json.loads(await tools["playbook_dry_run"](name="greeter"))
    assert out["tested_version"] == 2
    assert out["is_candidate"] is True
    tested = runner.dry_ran[-1]
    assert tested.definition["steps"][0]["args"] == {"message": "{{ inputs.name }}"}

    out = json.loads(await tools["playbook_dry_run"](name="greeter", version="live"))
    assert out["tested_version"] == 1
    assert runner.dry_ran[-1].definition["steps"][0]["args"] == {
        "message": "{{ inputs.greeting }}",
    }


@pytest.mark.asyncio
async def test_dry_run_without_candidate_uses_live(env):
    sf, tools, runner, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_dry_run"](name="greeter"))
    assert out["tested_version"] == 1
    assert out["is_candidate"] is False
    out = json.loads(await tools["playbook_dry_run"](name="greeter", version="candidate"))
    assert "no candidate" in out["error"]


# --- promote ---

@pytest.mark.asyncio
async def test_promote_swaps_live_and_records_lineage(env):
    sf, tools, _, bus = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await _save_candidate(tools)
    await _green_run(sf, 2)
    bus.events.clear()
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert out["status"] == "published"
    assert out["live_version"] == 2
    assert out["previous_live_version"] == 1
    assert [g["gate"] for g in out["gates"]] == [
        "static_validation", "specs", "test_run", "manifest_drift", "probes",
    ]
    assert all(g["ok"] for g in out["gates"])
    pb = await _get(sf)
    assert pb.code == NEW_CODE                  # candidate content is live now
    assert pb.live_version == 2
    assert pb.candidate_version is None
    rows = await _rows(sf)
    assert rows[2].promoted_from == 1           # rollback lineage
    assert any(n == "playbook.saved" for n, _ in bus.events)  # triggers resync


@pytest.mark.asyncio
async def test_promote_without_candidate_is_refused(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert "no candidate" in out["error"]


@pytest.mark.asyncio
async def test_promote_gate_names_static_validation_failure(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await _save_candidate(tools)
    # corrupt the candidate row so validation fails (undefined step reference)
    async with sf() as s:
        row = (await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.version == 2)
        )).scalar_one()
        bad = dict(row.definition)
        bad["steps"] = [{
            "id": "say", "kind": "tool_call", "tool": "send_chat_message",
            "args": {"message": "{{ steps.missing.out }}"},
        }]
        row.definition = bad
        await s.commit()
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert "static_validation" in out["error"]
    assert out["gate"] == "static_validation"
    assert out["issues"]
    pb = await _get(sf)
    assert pb.live_version == 1                 # live untouched
    assert pb.candidate_version == 2            # candidate kept for fixing


@pytest.mark.asyncio
async def test_promote_keeps_live_manifest(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](
        name="greeter", code=CODE, manifest="## Purpose\nGreets.\n",
    )
    await _save_candidate(tools)
    await _green_run(sf, 2)
    out = json.loads(await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert out["status"] == "published"
    pb = await _get(sf)
    assert pb.manifest == "## Purpose\nGreets.\n"


# --- rollback ---

@pytest.mark.asyncio
async def test_rollback_restores_previous_live(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await _save_candidate(tools)
    await _green_run(sf, 2)
    await tools["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools))
    # rollback publishes v1 through the same gate — its live history is
    # the evidence.
    await _green_run(sf, 1, is_test=False)
    out = json.loads(await tools["playbook_rollback"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert out["status"] == "rolled_back"
    assert out["live_version"] == 1
    assert out["previous_live_version"] == 2
    pb = await _get(sf)
    assert pb.code == CODE
    assert pb.live_version == 1
    rows = await _rows(sf)
    assert rows[2].code == NEW_CODE             # rolled-back version kept


@pytest.mark.asyncio
async def test_rollback_without_history_is_refused(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_rollback"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(tools)))
    assert "no previous version" in out["error"]


# --- candidate run + live run ---

@pytest.mark.asyncio
async def test_run_candidate_executes_the_candidate_shim(env):
    sf, tools, runner, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await _save_candidate(tools)
    out = json.loads(await tools["playbook_run_candidate"](name="greeter"))
    assert out["candidate_version"] == 2
    assert "live playbook is unchanged" in out["note"]
    shim, trigger = runner.started[-1]
    assert trigger == "agent-candidate"
    assert shim.definition["steps"][0]["args"] == {"message": "{{ inputs.name }}"}
    assert shim.live_version == 2               # run history records v2
    pb = await _get(sf)
    assert pb.code == CODE                      # live never touched


@pytest.mark.asyncio
async def test_run_candidate_without_candidate_is_refused(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_run_candidate"](name="greeter"))
    assert "no candidate" in out["error"]


@pytest.mark.asyncio
async def test_live_run_notes_pending_candidate(env):
    sf, tools, runner, _ = env
    await tools["playbook_propose"](
        name="greeter", code=CODE, agent_autonomy="agent_may_trigger",
    )
    await _save_candidate(tools)
    out = json.loads(await tools["playbook_run"](name="greeter"))
    assert "LIVE version (1)" in out["note"]
    assert "playbook_publish" in out["note"]
    live, trigger = runner.started[-1]
    assert live.definition["steps"][0]["args"] == {"message": "{{ inputs.greeting }}"}


# --- manifest set with a pending candidate (version-uniqueness invariant) ---

@pytest.mark.asyncio
async def test_manifest_set_with_pending_candidate_keeps_versions_unique(env):
    sf, tools, _, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await _save_candidate(tools)                # candidate = v2
    out = json.loads(await tools["playbook_manifest_set"](
        name="greeter", manifest="## Purpose\nGreets.\n",
    ))
    assert out["version"] == 3                  # bumped PAST the candidate
    pb = await _get(sf)
    assert pb.live_version == 3
    assert pb.candidate_version == 2            # candidate survives
    rows = await _rows(sf)
    assert set(rows) == {1, 2, 3}               # no duplicate version numbers
    assert rows[3].manifest == "## Purpose\nGreets.\n"


# --- policies ---

def test_new_tool_policies():
    tds = {td.name: td for td, _ in build_tools(None, _Bus(), _Runner())}
    # plans/018 phase 1: publish/rollback raise their own owner approval in
    # the handler (one rich card per change) — core policy is auto_approve
    # so the per-call prompt doesn't double-ask.
    for name in ("playbook_publish", "playbook_rollback"):
        assert tds[name].policy == "auto_approve", name
        assert tds[name].risk_level == "medium", name
        assert "explanation" in tds[name].parameters["required"], name
    assert tds["playbook_run_candidate"].policy == "prompt_always"
    assert tds["playbook_run_candidate"].risk_level == "medium"
    assert "version" in tds["playbook_dry_run"].parameters["properties"]
