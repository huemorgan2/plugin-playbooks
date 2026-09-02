"""0.39.0 (plans/022) — truthful evidence, fail-closed approvals, spec
provenance, coding-agent reads, honest dry runs, history integrity.

Meltdown 2026-09-01/02: a FAILED run was announced as green evidence, a
broken approval wait read as approval, carried specs vanished silently, the
agent could not read old versions, dry runs green-lit nonexistent tools, and
the duplicate-row healer kept rows by age instead of content.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from evidence import EXPLANATION, green_run
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import publish, versioning
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import (
    Base, Playbook, PlaybookRun, PlaybookSpec, PlaybookStepRun, PlaybookVersion,
)
from plugin_playbooks.runner import PlaybookRunner, _coerce_inputs

NOW = datetime.now(timezone.utc)

CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")


# --- shared plumbing --------------------------------------------------------

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
    def __init__(self, decision="approved"):
        self.requests: list[dict] = []
        self._decision = decision

    async def request(self, **kw):
        self.requests.append(kw)
        return _Decision(self._decision)

    async def record_auto_approval(self, **kw):
        pass


class _ExplodingApprovals(_Approvals):
    async def request(self, **kw):
        raise TimeoutError("approval bus down")


OPS = uuid.uuid4()


class _Ctx:
    def __init__(self, approvals):
        self.approval = approvals
        self.sent: list[tuple[str, str]] = []

    async def ops_conversation_id(self):
        return OPS

    def conversation_state(self):
        return None

    async def send_muted_message(self, title, body, **kw):
        self.sent.append((title, body))


class _NoApprovalCtx(_Ctx):
    """A LIVE context whose approval engine is broken/unwired."""

    def __init__(self):
        super().__init__(None)
        del self.approval  # attribute access raises → engine unavailable

    def __getattr__(self, item):
        if item == "approval":
            raise RuntimeError("approvals not wired")
        raise AttributeError(item)


async def _env(ctx):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    bus = _Bus()
    tools = {td.name: h for td, h in build_tools(sf, bus, _StubRunner(), ctx)}
    return engine, sf, tools, bus


async def _candidate(sf, tools) -> None:
    await tools["playbook_propose"](name="greeter", code=CODE)
    read = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    await tools["playbook_edit"](
        name="greeter", ticket=read["ticket"], code=NEW_CODE,
    )


async def _pb(sf) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one()


async def _failed_run(sf, version: int, error: str = "boom") -> uuid.UUID:
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalars().first()
        run = PlaybookRun(
            playbook_id=pb.id, playbook_version=version, status="failed",
            trigger="agent-candidate", is_test=True, started_at=later,
            completed_at=later + timedelta(seconds=1),
        )
        s.add(run)
        await s.flush()
        s.add(PlaybookStepRun(
            run_id=run.id, step_id="say", step_kind="tool_call",
            status="failed", error=error, inputs={"message": "hi"},
            started_at=later,
        ))
        await s.commit()
        return run.id


# --- P1: a failed run is never evidence -------------------------------------

@pytest.mark.asyncio
async def test_gate_puts_failed_run_in_the_failed_slot_only():
    approvals = _Approvals()
    engine, sf, tools, _ = await _env(_Ctx(approvals))
    try:
        await _candidate(sf, tools)
        run_id = await _failed_run(sf, 2)
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            row = (await s.execute(
                select(PlaybookVersion).where(PlaybookVersion.version == 2)
            )).scalar_one()
            gate, refusal, evidence, failed = await publish.test_run_gate(
                s, pb.id, 2, row.created_at, require=False,
            )
        assert evidence is None            # NEVER a failed run
        assert failed is not None and failed.id == run_id
        assert gate["ok"] is False and gate["enforced"] is False
        assert refusal is None             # unenforced → reported, not refused
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_result_and_announce_state_failed_evidence_honestly():
    # Gate off (unenforced) + latest run FAILED → publish goes through but
    # every surface says FAILED, none says green.
    approvals = _Approvals()
    ctx = _Ctx(approvals)
    engine, sf, tools, bus = await _env(ctx)
    try:
        await _candidate(sf, tools)
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            pb.publish_require_run = False  # Settings → Publish, gate off
            await s.commit()
        run_id = await _failed_run(sf, 2)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out["status"] == "published"
        assert out["evidence"]["status"] == "failed"
        assert out["evidence"]["run_id"] == str(run_id)
        # the approval card's EVIDENCE line said FAILED, not green (the
        # agent's own explanation text may say anything)
        pres = approvals.requests[0]["presentation"]
        ev_line = [l for l in pres["explanation"].splitlines()
                   if l.startswith("Evidence:")][0]
        assert "FAILED" in ev_line and "green" not in ev_line.lower()
        # the ops announce's evidence line said FAILED, not green
        announce = "\n".join(body for _, body in ctx.sent)
        ann_line = [l for l in announce.splitlines()
                    if l.startswith("Test evidence:")][0]
        assert "FAILED" in ann_line and "green" not in ann_line.lower()
        # the bus event carries evidence_status=failed
        pub = [p for n, p in bus.events if n == "playbook.published"][0]
        assert pub["evidence_status"] == "failed"
        assert pub["latest_run_id"] == str(run_id)
        assert "evidence_run_id" not in pub
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_green_publish_reports_passed_evidence():
    approvals = _Approvals()
    ctx = _Ctx(approvals)
    engine, sf, tools, bus = await _env(ctx)
    try:
        await _candidate(sf, tools)
        await green_run(sf, 2)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out["status"] == "published"
        assert out["evidence"]["status"] == "passed"
        assert out["evidence"]["run_id"]
        pub = [p for n, p in bus.events if n == "playbook.published"][0]
        assert pub["evidence_status"] == "passed"
    finally:
        await engine.dispose()


# --- P2: approvals fail CLOSED ----------------------------------------------

@pytest.mark.asyncio
async def test_broken_approval_engine_aborts_publish():
    engine, sf, tools, _ = await _env(_NoApprovalCtx())
    try:
        await _candidate(sf, tools)
        await green_run(sf, 2)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert "Approval not obtained" in out["error"]
        pb = await _pb(sf)
        assert pb.live_version == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_approval_wait_exception_aborts_publish():
    engine, sf, tools, _ = await _env(_Ctx(_ExplodingApprovals()))
    try:
        await _candidate(sf, tools)
        await green_run(sf, 2)
        out = json.loads(await tools["playbook_publish"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert "Approval not obtained" in out["error"]
        assert "TimeoutError" in out["error"]
        pb = await _pb(sf)
        assert pb.live_version == 1
    finally:
        await engine.dispose()


# --- P3: carried specs carry provenance -------------------------------------

@pytest.mark.asyncio
async def test_carried_specs_keep_original_author_version():
    engine, sf, tools, _ = await _env(_Ctx(_Approvals()))
    try:
        await _candidate(sf, tools)  # v2 candidate
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            s.add(PlaybookSpec(
                playbook_id=pb.id, playbook_version=2, name="greets",
                spec={"given": {}, "expect": []}, created_by="agent",
            ))
            await s.commit()
        # two more mints: v2 → v3 → v4; carried_from must stay 2 (original)
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            await versioning.mint_version(
                s, pb, definition=pb.definition, code=pb.code,
                manifest=pb.manifest or "", author="agent", message="v3",
                source_version=2,
            )
            await s.commit()
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            await versioning.mint_version(
                s, pb, definition=pb.definition, code=pb.code,
                manifest=pb.manifest or "", author="agent", message="v4",
                source_version=3,
            )
            await s.commit()
            v4_spec = (await s.execute(
                select(PlaybookSpec).where(PlaybookSpec.playbook_version == 4)
            )).scalar_one()
        assert v4_spec.spec["carried_from"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_deleting_a_carried_spec_requires_a_reason():
    engine, sf, tools, _ = await _env(_Ctx(_Approvals()))
    try:
        await _candidate(sf, tools)
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            s.add(PlaybookSpec(
                playbook_id=pb.id, playbook_version=2, name="greets",
                spec={"given": {}, "expect": [], "carried_from": 1},
                created_by="agent",
            ))
            await s.commit()
        out = json.loads(await tools["playbook_spec_delete"](
            name="greeter", spec_name="greets", version="2",
        ))
        assert "requires a reason" in out["error"]
        out = json.loads(await tools["playbook_spec_delete"](
            name="greeter", spec_name="greets", version="2",
            why="tool was removed from the playbook",
        ))
        assert out["status"] == "deleted"
        assert out["carried_from"] == 1
        listed = json.loads(await tools["playbook_spec_list"](
            name="greeter", version="2",
        ))
        assert all(s_["name"] != "greets" for s_ in listed["specs"])
    finally:
        await engine.dispose()


# --- P4: coding-agent-grade reads -------------------------------------------

@pytest.mark.asyncio
async def test_version_history_read_and_diff():
    engine, sf, tools, _ = await _env(_Ctx(_Approvals()))
    try:
        await _candidate(sf, tools)  # v1 live, v2 candidate
        listing = json.loads(await tools["playbook_versions"](name="greeter"))
        assert listing["count"] == 2
        assert listing["live_version"] == 1
        assert listing["candidate_version"] == 2
        v2 = [v for v in listing["versions"] if v["version"] == 2][0]
        assert v2["candidate"] is True and v2["has_code"] is True

        read = json.loads(await tools["playbook_version_read"](
            name="greeter", version=1,
        ))
        assert "inputs.greeting" in read["code"]
        assert read["live"] is True
        read2 = json.loads(await tools["playbook_version_read"](
            name="greeter", version=2,
        ))
        assert "inputs.name" in read2["code"]

        diff = json.loads(await tools["playbook_version_diff"](
            name="greeter", from_version=1, to_version=2,
        ))
        assert "-say = tool('send_chat_message', message=inputs.greeting)" in diff["code_diff"]
        assert "+say = tool('send_chat_message', message=inputs.name)" in diff["code_diff"]

        missing = json.loads(await tools["playbook_version_read"](
            name="greeter", version=9,
        ))
        assert "no stored version 9" in missing["error"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_runs_read_returns_full_failure_output():
    engine, sf, tools, _ = await _env(_Ctx(_Approvals()))
    try:
        await _candidate(sf, tools)
        long_error = "TypeError: unsupported operand — " + "x" * 5000
        run_id = await _failed_run(sf, 2, error=long_error)
        out = json.loads(await tools["playbook_runs"](
            name="greeter", version=2, status="failed",
        ))
        assert out["count"] == 1
        run = out["runs"][0]
        assert run["run_id"] == str(run_id)
        assert run["failures"][0]["step_id"] == "say"
        assert run["failures"][0]["error"] == long_error  # FULL, untruncated
    finally:
        await engine.dispose()


# --- P5: honest dry runs ----------------------------------------------------

def _bare_runner(tools: dict) -> PlaybookRunner:
    class _Reg:
        def get(self, name):
            if name not in tools:
                raise KeyError(name)
            return tools[name]

    r = PlaybookRunner.__new__(PlaybookRunner)
    r._tools = _Reg()
    return r


def _dry_playbook(tool: str):
    from types import SimpleNamespace
    return SimpleNamespace(
        name="greeter",
        definition={
            "name": "greeter", "description": "says hi",
            "steps": [{"id": "say", "kind": "tool_call", "tool": tool,
                       "args": {"message": "hi"}}],
        },
        inputs_schema=None,
    )


@pytest.mark.asyncio
async def test_dry_run_fails_on_unknown_tool_even_when_stubbed():
    runner = _bare_runner({})
    out = await runner.dry_run(_dry_playbook("no_such_tool"),
                               stubs={"say": {"ok": True}})
    assert out["status"] == "failed"
    assert "unknown tool 'no_such_tool'" in out["error"]


@pytest.mark.asyncio
async def test_dry_stub_of_known_tool_self_describes():
    runner = _bare_runner({"send_chat_message": object()})
    out = await runner.dry_run(_dry_playbook("send_chat_message"))
    assert out["status"] == "done"
    say = out["references"]["say"]
    assert say["result"]["_note"] == "simulated — tool was NOT called"


# --- P5b: typed trigger inputs ----------------------------------------------

def test_coerce_inputs_casts_scalars_to_declared_types():
    from types import SimpleNamespace
    pb = SimpleNamespace(inputs_schema={"type": "object", "properties": {
        "item_id": {"type": "string"},
        "count": {"type": "integer"},
        "enabled": {"type": "boolean"},
    }})
    out = _coerce_inputs(pb, {
        "item_id": 5021314523,       # the meltdown poison: int where string declared
        "count": "7",
        "enabled": "true",
        "extra": [1, 2],             # undeclared → untouched
    })
    assert out["item_id"] == "5021314523"
    assert out["count"] == 7
    assert out["enabled"] is True
    assert out["extra"] == [1, 2]


def test_coerce_inputs_leaves_uncoercible_values_alone():
    from types import SimpleNamespace
    pb = SimpleNamespace(inputs_schema={"type": "object", "properties": {
        "count": {"type": "integer"},
    }})
    out = _coerce_inputs(pb, {"count": "not-a-number"})
    assert out["count"] == "not-a-number"


# --- P6: history integrity --------------------------------------------------

@pytest.mark.asyncio
async def test_healer_and_reader_keep_the_content_bearing_duplicate():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sf() as s:
            pb = Playbook(
                name="greeter", display_name="g", description="d",
                definition={"name": "greeter", "steps": []}, version=3,
                live_version=3, status="enabled",
            )
            s.add(pb)
            await s.flush()
            # OLDER row: empty shell (the meltdown's v38 mutilation shape)
            s.add(PlaybookVersion(
                playbook_id=pb.id, version=3,
                definition={"name": "greeter", "steps": []}, code=None,
                author="system", message="shell",
                created_at=NOW - timedelta(days=2),
            ))
            # NEWER row: real content
            s.add(PlaybookVersion(
                playbook_id=pb.id, version=3,
                definition={"name": "greeter", "steps": [
                    {"id": "say", "kind": "tool_call",
                     "tool": "send_chat_message", "args": {"message": "hi"}},
                ]},
                code="playbook(name='greeter')\n", manifest="# m",
                author="agent", message="real",
                created_at=NOW - timedelta(days=1),
            ))
            await s.commit()
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            picked = await versioning.get_version_row(s, pb, 3)
            assert picked.code  # content wins over age
        deleted = await versioning.heal_duplicate_version_rows(sf)
        assert deleted == 1
        async with sf() as s:
            survivor = (await s.execute(
                select(PlaybookVersion).where(PlaybookVersion.version == 3)
            )).scalar_one()
        assert survivor.code and survivor.manifest == "# m"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_restore_of_a_manifestless_row_keeps_the_live_manifest():
    engine, sf, tools, _ = await _env(_Ctx(_Approvals()))
    try:
        await _candidate(sf, tools)
        await green_run(sf, 2)
        await tools["playbook_publish"](name="greeter", explanation=EXPLANATION)
        async with sf() as s:
            pb = (await s.execute(select(Playbook))).scalar_one()
            pb.manifest = "# the live manifest"
            row = (await s.execute(
                select(PlaybookVersion).where(PlaybookVersion.version == 1)
            )).scalar_one()
            # legacy rows carry no manifest snapshot (Scanny's NULLs predate
            # the column's NOT NULL); empty is the same falsy shape here
            row.manifest = ""
            await s.commit()
        await green_run(sf, 1)
        out = json.loads(await tools["playbook_rollback"](
            name="greeter", explanation=EXPLANATION,
        ))
        assert out.get("status") in ("published", "rolled_back"), out
        pb = await _pb(sf)
        assert pb.live_version == 1
        assert pb.manifest == "# the live manifest"  # NOT nulled
    finally:
        await engine.dispose()
