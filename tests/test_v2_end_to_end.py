"""plans/032 phase 11 (part b) — the scripted end-to-end story through the
REAL handlers: propose (python, validated on save) → dry run (simulated,
no row) → real candidate run (side effects, one `is_test` row) →
approval-gated publish (exactly one owner card) — with no `playbook_validate`
call anywhere and every run-shaped result opening with the phase-09
provenance envelope. The turn runs as a delegation: the `_delegation_id`
ContextVar is set the way `_drive_delegation` sets it around `run_turn`
(`tests/test_v2_delegation.py::test_identity_set_inside_run_turn` proves the
real chain), so the live row is stamped `delegation:<uuid>`.

Harness: `tests/v2harness.py`'s env on a FILE-backed sqlite (a real
`PlaybookRunner` drives the candidate run from its own task while the
handler waits — the Post-M3 rule), the scripted `code_run` fake of
`tests/test_v2_loop.py` (no jail), a recording `file_write` fake, the
lifecycle repro's `_Ctx` / `_Approvals`. The canvas step is the plugin/10
REST check: the graph lists the checker's call site and the run's trace
ends on it.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from evidence import EXPLANATION
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_repro_fixplaybooks_lifecycle import _Approvals, _Ctx
from test_v2_loop import ScriptedCodeRun, _effect, _Tool, _Tools
from v2harness import Bus, Env

from plugin_playbooks import _ensure_columns, routes
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.delegation import _delegation_id, writer_identity
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookVersion
from plugin_playbooks.provenance import ENVELOPE_KEYS
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.v2.checker import check

BASE = "/api/p/plugin-playbooks"
NAME = "pb-e2e"

# Not the master §2 example (its `ctx.approve` would raise a second card —
# plan Risks 12): one real side effect, one return value.
E2E_CODE = (
    "async def run(ctx, inputs):\n"
    "    await ctx.tool('file_write', path=inputs['path'], content=inputs['note'])\n"
    "    return {'written': inputs['path']}\n"
)
INPUTS_SCHEMA = {
    "type": "object",
    "properties": {"path": {"type": "string"}, "note": {"type": "string"}},
    "required": ["path", "note"],
}
INPUTS = {"path": "/tmp/e2e-note.txt", "note": "hello from the candidate run"}
ENVELOPE = list(ENVELOPE_KEYS)
PROPOSE_KEYS = [
    "playbook_id", "name", "format", "status", "live_version", "candidate_version",
    "runnable_via", "triggers_active", "publish_required", "validated", "warnings", "next",
]


def _script(env: dict) -> dict:
    """What the real shim would do for E2E_CODE: one `file_write` effect
    with the run's inputs (journal entry 0), then the return value."""
    journal = env["journal"]
    inputs = journal[0]["inputs"]
    site = env["call_sites"][0]["id"]
    if len(journal) == 1:
        return _effect(1, site, 1, "tool", "file_write",
                       {"path": inputs["path"], "content": inputs["note"]})
    return {"kind": "return", "value": {"written": inputs["path"]}}


class _FileWrite:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kw) -> dict:
        self.calls.append(dict(kw))
        return {"ok": True, "path": kw.get("path")}


async def _env(tmp_path, *, decision: str = "approved") -> tuple[Env, _FileWrite]:
    # v2harness.env on a file-backed engine (a run task + the test task).
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/e2e.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_columns(engine)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    file_write = _FileWrite()
    registry = _Tools(file_write=_Tool(file_write))
    code_run = ScriptedCodeRun(_script)
    registry.add("code_run", code_run.handler)
    bus = Bus()
    runner = PlaybookRunner(session_factory=sf, tool_registry=registry, events=bus)
    approvals = _Approvals(decision=decision)
    ctx = _Ctx(approvals)
    pairs = build_tools(sf, bus, runner, ctx)
    tools = {td.name: h for td, h in pairs}
    defs = {td.name: td for td, _ in pairs}
    return Env(engine, sf, tools, defs, bus, runner, code_run, approvals, ctx, registry), file_write


class ScriptedAgent:
    """The delegate's "turn": an ordered list of real handler calls under
    `_delegation_id` = this delegation's id, each recorded as (name, result).
    The ContextVar is set exactly as `_drive_delegation` sets it around
    `ctx.agent.run_turn(...)` (set before, reset in `finally`)."""

    def __init__(self, env: Env, did: uuid.UUID) -> None:
        self.env = env
        self.did = did
        self.calls: list[tuple[str, dict]] = []
        self.identities: list[str] = []

    async def call(self, tool: str, /, **kw) -> dict:
        assert tool in self.env.tools, tool
        self.identities.append(writer_identity())
        out = json.loads(await self.env.tools[tool](**kw))
        self.calls.append((tool, out))
        return out

    async def turn(self, steps) -> None:
        token = _delegation_id.set(self.did)
        try:
            await steps(self)
        finally:
            _delegation_id.reset(token)

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def texts(self) -> list[str]:
        return [json.dumps(r) for _, r in self.calls]


async def _run_rows(sf) -> list[PlaybookRun]:
    async with sf() as s:
        return list((await s.execute(select(PlaybookRun))).scalars().all())


async def _playbook(sf, name: str) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()


async def _version_row(sf, name: str, n: int) -> PlaybookVersion:
    pb = await _playbook(sf, name)
    async with sf() as s:
        return (await s.execute(
            select(PlaybookVersion).where(
                PlaybookVersion.playbook_id == pb.id, PlaybookVersion.version == n,
            )
        )).scalar_one()


def _no_validate_anywhere(agent: ScriptedAgent) -> None:
    assert "playbook_validate" not in agent.names(), agent.names()
    for name, out in agent.calls:
        for key in ("next", "hint", "message", "note"):
            text = out.get(key)
            if isinstance(text, str):
                assert "playbook_validate" not in text, (name, key, text)


async def _story(agent: ScriptedAgent, file_write: _FileWrite, sf) -> None:
    """propose → dry run → real candidate run → (status) → publish."""
    # 1. WRITE — a green write is validated on save.
    out = await agent.call(
        "playbook_propose", name=NAME, format="python", code=E2E_CODE,
        inputs_schema=json.dumps(INPUTS_SCHEMA),
    )
    assert out["status"] == "candidate_saved" and out["validated"] is True, out
    assert out["format"] == "python" and out["candidate_version"] == 1
    assert out["live_version"] is None and out["publish_required"] is True
    assert list(out) == PROPOSE_KEYS, list(out)
    assert "playbook_validate" not in out["next"] and "playbook_run_candidate" in out["next"]
    assert (await _run_rows(sf)) == []
    assert (await _version_row(sf, NAME, 1)).author == f"delegation:{agent.did}"

    # 2. DRY RUN — simulated, no row, no side effect, envelope first.
    out = await agent.call("playbook_dry_run", name=NAME, inputs=json.dumps(INPUTS))
    assert list(out)[:5] == ENVELOPE, list(out)
    assert out["kind"] == "dry_run" and out["side_effects"] is False
    assert out["version"] == 1 and out["version_role"] == "candidate" and out["run_id"] is None
    assert out["status"] == "simulated" and out["dry_run"] is True, out
    assert out["unreached_call_sites"] == [], out
    assert (await _run_rows(sf)) == [] and file_write.calls == []

    # 3. PROOF RUN — real side effects, one is_test row, envelope first.
    out = await agent.call(
        "playbook_run_candidate", name=NAME, inputs=json.dumps(INPUTS), wait_seconds=30,
    )
    assert list(out)[:5] == ENVELOPE, list(out)
    assert out["kind"] == "candidate_test_run" and out["side_effects"] is True
    assert out["version"] == 1 and out["version_role"] == "candidate"
    assert out["status"] == "done", out
    assert out["result"] == {"written": INPUTS["path"]}, out
    run_id = out["run_id"]
    uuid.UUID(run_id)
    rows = await _run_rows(sf)
    assert len(rows) == 1 and str(rows[0].id) == run_id
    assert rows[0].is_test is True and rows[0].trigger == "agent-candidate"
    assert rows[0].status == "done"
    assert file_write.calls == [{"path": INPUTS["path"], "content": INPUTS["note"]}]

    # 3b. playbook_status on the finished run — same envelope, same row rule.
    out = await agent.call("playbook_status", run_id=run_id)
    assert list(out)[:5] == ENVELOPE, list(out)
    assert out["kind"] == "candidate_test_run" and out["run_id"] == run_id
    assert out["status"] == "done" and out["version"] == 1

    # 4. PUBLISH — owner intent stated; the gate raises exactly one card.
    out = await agent.call("playbook_publish", name=NAME, explanation=EXPLANATION)
    agent.publish = out  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_agent_turn_proposes_dry_runs_runs_and_publishes(tmp_path):
    e, file_write = await _env(tmp_path)
    did = uuid.uuid4()
    agent = ScriptedAgent(e, did)
    try:
        await agent.turn(lambda a: _story(a, file_write, e.sf))
        assert writer_identity() == "agent"  # the token was reset
        assert set(agent.identities) == {f"delegation:{did}"}

        out = agent.publish  # type: ignore[attr-defined]
        assert out["status"] == "published", out
        assert out["live_version"] == 1 and out["previous_live_version"] is None, out
        assert list(out) == [
            "playbook", "status", "live_version", "previous_live_version",
            "gates", "evidence", "note", "next",
        ], list(out)
        # the evidence is THE candidate run of step 3 — the gate saw the row
        run_id = agent.calls[2][1]["run_id"]
        assert out["evidence"] == {"run_id": run_id, "status": "passed"}, out
        assert [g["gate"] for g in out["gates"]] == ["static_validation", "test_run", "probes"]
        assert all(g["ok"] for g in out["gates"]), out["gates"]
        assert "playbook_validate" not in out["next"]
        assert len(e.approvals.requests) == 1, e.approvals.requests
        pb = await _playbook(e.sf, NAME)
        assert (pb.live_version, pb.candidate_version) == (1, None)
        # publish moves the pointer onto the delegated candidate row — no
        # new row is minted, so the LIVE row carries the delegation stamp.
        live = await _version_row(e.sf, NAME, 1)
        assert live.author == f"delegation:{did}"
        assert live.author.startswith("delegation:") and len(live.author) == 47
        overview = json.loads(await e.tools["playbook_overview"](name=NAME))
        assert overview["candidate"] is None
        assert overview["playbook_run_executes"]["version"] == 1
        assert overview["versions"][0]["author"] == f"delegation:{did}"
        assert overview["versions"][0]["live"] is True

        # Across the script: no validate call, none asked for.
        assert agent.names() == [
            "playbook_propose", "playbook_dry_run", "playbook_run_candidate",
            "playbook_status", "playbook_publish",
        ]
        _no_validate_anywhere(agent)
        # one row over the whole story: the dry run wrote none, the
        # candidate run wrote one, publish wrote none.
        rows = await _run_rows(e.sf)
        assert len(rows) == 1 and rows[0].is_test is True

        # Canvas step (plugin/10): the graph lists the checker's call site
        # and the journaled run's trace ends on it.
        call_sites = check(E2E_CODE, name=NAME, version=1).summary["call_sites"]
        assert [c["id"] for c in call_sites] == ["file_write"]
        routes.init_routes(e.sf, runner=e.runner)
        app = FastAPI()
        app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
        app.include_router(routes.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://luna.test",
        ) as c:
            g = await c.get(f"{BASE}/playbooks/{NAME}/graph", params={"version": 1})
            assert g.status_code == 200, g.text
            graph = g.json()
            assert graph["format"] == "python" and graph["version"] == 1
            assert {f"step-{s['id']}" for s in call_sites} <= set(graph["node_ids"])
            r = await c.get(f"{BASE}/playbooks/runs/{rows[0].id}")
            assert r.status_code == 200, r.text
            run = r.json()
            assert run["status"] == "done" and run["trace"], run
            assert run["trace"][-1]["node"] == "step-file_write"
            assert run["trace"][-1]["status"] == "done"
            assert run["failed_line"] is None and run["error"] is None
            v = await c.get(f"{BASE}/playbooks/{NAME}/versions")
            assert v.status_code == 200, v.text
            assert {x["version"]: x["author"] for x in v.json()} == {1: f"delegation:{did}"}
    finally:
        await asyncio.sleep(0.05)
        await e.dispose()


@pytest.mark.asyncio
async def test_rejected_card_publishes_nothing(tmp_path):
    e, file_write = await _env(tmp_path, decision="rejected")
    did = uuid.uuid4()
    agent = ScriptedAgent(e, did)
    try:
        await agent.turn(lambda a: _story(a, file_write, e.sf))
        out = agent.publish  # type: ignore[attr-defined]
        assert out.get("status") != "published", out
        assert len(e.approvals.requests) == 1, e.approvals.requests
        pb = await _playbook(e.sf, NAME)
        assert pb.live_version in (0, None) and pb.candidate_version == 1
        overview = json.loads(await e.tools["playbook_overview"](name=NAME))
        assert overview["playbook_run_executes"]["version"] is None
        assert overview["candidate"]["version"] == 1
        assert overview["candidate"]["author"] == f"delegation:{did}"
        # the candidate run stays the only row; the rejection minted nothing
        rows = await _run_rows(e.sf)
        assert len(rows) == 1 and rows[0].is_test is True
        assert file_write.calls == [{"path": INPUTS["path"], "content": INPUTS["note"]}]
        _no_validate_anywhere(agent)
    finally:
        await asyncio.sleep(0.05)
        await e.dispose()
