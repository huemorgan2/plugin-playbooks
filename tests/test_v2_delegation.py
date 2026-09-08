"""plans/032 phase 11 (part a) — writer identity, author stamping, the
candidate-conflict guard, the v2 delegate prompt, the steering text.

- `writer_identity()` is `agent` outside a delegation and `delegation:<id>`
  inside `_drive_delegation`'s `run_turn` call chain (a ContextVar).
- The three mint sites (`_edit_impl`, `_propose`, `_manifest_set`) stamp it
  on the version row; `playbook_versions` and REST `list_versions` echo it.
- Saving over ANOTHER author's unpublished candidate is refused before the
  ticket is consumed, naming the author and version; the same author still
  iterates on its own candidate; `replace_candidate=true` is the explicit,
  owner-authorised way through and the row's message says who was replaced.
- `_delegate_prompt(task, pb, format=)` follows the target's format: python
  (v2) for new playbooks and python targets, pblang (byte-identical to the
  phase-00 text — tests/test_delegate_prompt.py) for pblang targets.

Harness: file-backed sqlite (a delegation drives tool handlers from its own
task while the test polls), the lifecycle repro's `_Ctx` / `_Approvals` /
`_StubRunner` (registry None → the checker skips its unknown-tool rule).
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid

import httpx
import pytest
from evidence import EXPLANATION, green_run
from fastapi import FastAPI
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_delegation import FakeAgent, FakeCtx
from test_repro_fixplaybooks_lifecycle import _Approvals, _Bus, _Ctx, _StubRunner

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import _DELEGATION_SKILL_BODY, PlaybooksPlugin, routes
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.delegation import (
    _PROMPT_TAIL,
    _PROMPT_TAIL_V2,
    _TASKS,
    V2_PROMPT_MARKER,
    _delegate_prompt,
    _delegation_id,
    build_delegation_tools,
    delegate_toolset,
    writer_identity,
)
from plugin_playbooks.models import Base, Playbook, PlaybookDelegation, PlaybookVersion
from plugin_playbooks.v2.skill import V2_SKILL_BODY, V2_SKILL_MAX_BYTES

AUTHORING = PlaybooksPlugin.AUTHORING_TOOLS
BASE = "/api/p/plugin-playbooks"

PY_CODE = (
    "async def run(ctx, inputs):\n"
    "    say = await ctx.tool('echo', message=inputs['greeting'], _id='say')\n"
    "    return say\n"
)
PY_CODE_2 = PY_CODE.replace("inputs['greeting']", "inputs['name']")
PY_CODE_3 = PY_CODE.replace("inputs['greeting']", "inputs['nickname']")
PB_CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)


# ---- harness -----------------------------------------------------------------

class _Env:
    def __init__(self, engine, sf, tools, approvals):
        self.engine = engine
        self.sf = sf
        self.tools = tools
        self.approvals = approvals


@pytest.fixture
async def env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pb.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    approvals = _Approvals()
    pairs = build_tools(sf, _Bus(), _StubRunner(), _Ctx(approvals))
    tools = {td.name: h for td, h in pairs}
    yield _Env(engine, sf, tools, approvals)
    for t in list(_TASKS.values()):
        t.cancel()
    await asyncio.sleep(0)
    _TASKS.clear()
    await engine.dispose()


async def _playbook(sf, name: str) -> Playbook:
    async with sf() as s:
        return (await s.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one()


async def _rows(sf, name: str) -> dict[int, PlaybookVersion]:
    pb = await _playbook(sf, name)
    async with sf() as s:
        rows = (await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.playbook_id == pb.id)
        )).scalars().all()
    return {r.version: r for r in rows}


async def _propose_py(env: _Env, name: str = "greeter", code: str = PY_CODE) -> dict:
    return json.loads(await env.tools["playbook_propose"](
        name=name, code=code, format="python",
    ))


async def _publish_v1(env: _Env, name: str = "greeter") -> None:
    await green_run(env.sf, 1, name=name)
    out = json.loads(await env.tools["playbook_publish"](name=name, explanation=EXPLANATION))
    assert out.get("status") == "published", out


async def _read(env: _Env, name: str = "greeter") -> dict:
    return parse_read_stage(await env.tools["playbook_edit"](name=name))


async def _write(env: _Env, ticket: str, code: str, name: str = "greeter", **kw) -> dict:
    return json.loads(await env.tools["playbook_edit"](
        name=name, ticket=ticket, code=code, **kw,
    ))


async def _edit_as(env: _Env, did: uuid.UUID | None, code: str, name: str = "greeter") -> dict:
    """READ + WRITE with the ContextVar set to `did` (None = the agent)."""
    token = _delegation_id.set(did)
    try:
        read = await _read(env, name)
        return await _write(env, read["ticket"], code, name=name)
    finally:
        _delegation_id.reset(token)


async def _wait_settled(sf, delegation_id: str, timeout: float = 20.0) -> PlaybookDelegation:
    async def _poll():
        while True:
            async with sf() as s:
                row = await s.get(PlaybookDelegation, uuid.UUID(delegation_id))
            if row is not None and row.status != "running":
                return row
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(_poll(), timeout)


class _HandlerAgent(FakeAgent):
    """A ctx.agent whose "turn" calls real tool handlers — inside
    `_drive_delegation`'s `run_turn` call chain, as the core does."""

    def __init__(self, script, **kw) -> None:
        super().__init__(**kw)
        self.script_fn = script
        self.turn_results: list = []

    async def run_turn(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        self.turn_results.append(await self.script_fn())
        return self.result, {"total_tokens": 1}


def _label(did: uuid.UUID) -> str:
    return f"delegation {str(did)[:8]}"


# ---- identity ----------------------------------------------------------------

def test_writer_identity_default_is_agent():
    assert writer_identity() == "agent"
    did = uuid.uuid4()
    token = _delegation_id.set(did)
    try:
        assert writer_identity() == f"delegation:{did}"
    finally:
        _delegation_id.reset(token)
    assert writer_identity() == "agent"


@pytest.mark.asyncio
async def test_identity_set_inside_run_turn(env):
    seen: list[str] = []

    async def script():
        seen.append(writer_identity())
        return seen[-1]

    agent = _HandlerAgent(script, result="done")
    pairs = build_delegation_tools(FakeCtx(agent), env.sf, AUTHORING)
    by_name = {td.name: h for td, h in pairs}
    out = json.loads(await by_name["playbook_agent"](task="stamp me", wait_seconds=5))
    row = await _wait_settled(env.sf, out["delegation_id"])
    assert row.status == "done"
    assert seen == [f"delegation:{row.id}"]
    # the token was reset — the caller's context is untouched
    assert writer_identity() == "agent"


# ---- stamping ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_delegated_edit_stamps_delegation_author(env):
    out = await _propose_py(env)
    assert out["status"] == "candidate_saved" and out["validated"] is True
    rows = await _rows(env.sf, "greeter")
    assert rows[1].author == "agent"  # an inline propose stamps the agent
    await _publish_v1(env)

    results: list[dict] = []

    async def script():
        read = await _read(env)
        results.append(await _write(env, read["ticket"], PY_CODE_2))
        return results[-1]

    agent = _HandlerAgent(script, result="edited")
    by_name = {td.name: h for td, h in build_delegation_tools(FakeCtx(agent), env.sf, AUTHORING)}
    out = json.loads(await by_name["playbook_agent"](
        task="rename the input", playbook="greeter", wait_seconds=5,
    ))
    row = await _wait_settled(env.sf, out["delegation_id"])
    assert row.status == "done", row.result
    assert results and results[0]["status"] == "candidate_saved", results
    expected = f"delegation:{row.id}"

    rows = await _rows(env.sf, "greeter")
    assert rows[2].author == expected and rows[2].code == PY_CODE_2
    assert rows[1].author == "agent"
    pb = await _playbook(env.sf, "greeter")
    assert pb.candidate_version == 2 and pb.created_by == "agent"  # String(32): unchanged

    # the tool-side reader echoes it
    versions = json.loads(await env.tools["playbook_versions"](name="greeter"))
    by_n = {v["version"]: v for v in versions["versions"]}
    assert by_n[2]["author"] == expected and by_n[1]["author"] == "agent"
    # the truth surface reads the same row
    overview = json.loads(await env.tools["playbook_overview"](name="greeter"))
    assert overview["candidate"]["version"] == 2
    assert overview["candidate"]["author"] == expected

    # REST list_versions echoes it
    routes.init_routes(env.sf, runner=_StubRunner())
    app = FastAPI()
    app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://luna.test",
    ) as c:
        r = await c.get(f"{BASE}/playbooks/greeter/versions")
    assert r.status_code == 200, r.text
    listed = {v["version"]: v for v in r.json()}
    assert listed[2]["author"] == expected and listed[1]["author"] == "agent"

    # an inline (non-delegated) edit on its own candidate stamps `agent`
    await _propose_py(env, name="other", code=PY_CODE)
    out = await _edit_as(env, None, PY_CODE_2, name="other")
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 2
    rows = await _rows(env.sf, "other")
    assert rows[2].author == "agent"


@pytest.mark.asyncio
async def test_delegate_toolset_reads_python_summary(env):
    async with env.sf() as s:
        s.add(Playbook(
            name="pyfile", display_name="pyfile",
            definition={"name": "pyfile", "format": "python",
                        "tools": ["file_write", "send_chat_message"]},
            code="async def run(ctx, inputs):\n    return 1\n",
            format="python", status="enabled",
        ))
        s.add(Playbook(
            name="pbl", display_name="pbl",
            definition={"name": "pbl", "steps": [
                {"id": "a", "kind": "tool_call", "tool": "web_fetch", "args": {}},
            ]},
            status="enabled",
        ))
        await s.commit()
    py = await delegate_toolset(env.sf, "pyfile", AUTHORING)
    assert "file_write" in py
    assert "send_chat_message" not in py  # never, even when the playbook calls it
    assert set(AUTHORING) <= set(py)
    pbl = await delegate_toolset(env.sf, "pbl", AUTHORING)
    assert "web_fetch" in pbl and "file_write" not in pbl


# ---- the candidate-conflict guard --------------------------------------------

async def _foreign_candidate(env: _Env) -> uuid.UUID:
    """live v1 (agent) + candidate v2 written by a delegation."""
    await _propose_py(env)
    await _publish_v1(env)
    did = uuid.uuid4()
    out = await _edit_as(env, did, PY_CODE_2)
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 2, out
    rows = await _rows(env.sf, "greeter")
    assert rows[2].author == f"delegation:{did}"
    return did


@pytest.mark.asyncio
async def test_conflict_guard_names_the_author(env):
    did = await _foreign_candidate(env)
    read = await _read(env)
    out = await _write(env, read["ticket"], PY_CODE_3)
    assert out["saved"] is False and out["stage"] == "write"
    assert out["ticket_still_valid"] is True and out["ticket"] == read["ticket"]
    assert _label(did) in out["error"] and f"(delegation:{did})" in out["error"]
    assert "v2" in out["error"] and "never replaced silently" in out["error"]
    assert out["conflict"]["candidate_version"] == 2
    assert out["conflict"]["author"] == f"delegation:{did}"
    assert out["conflict"]["saved_at"]
    # nothing moved
    pb = await _playbook(env.sf, "greeter")
    assert pb.candidate_version == 2 and pb.live_version == 1
    rows = await _rows(env.sf, "greeter")
    assert set(rows) == {1, 2} and rows[2].code == PY_CODE_2
    # the ticket was not consumed: the same ticket still writes once the
    # owner authorises the replacement
    out = await _write(env, read["ticket"], PY_CODE_3, replace_candidate=True)
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 3


@pytest.mark.asyncio
async def test_same_author_resave_moves_pointer(env):
    await _propose_py(env)  # candidate v1 by the agent
    out = await _edit_as(env, None, PY_CODE_2)
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 2
    out = await _edit_as(env, None, PY_CODE_3)
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 3
    pb = await _playbook(env.sf, "greeter")
    assert pb.candidate_version == 3
    rows = await _rows(env.sf, "greeter")
    assert set(rows) == {1, 2, 3}
    assert rows[2].code == PY_CODE_2 and rows[3].code == PY_CODE_3  # old row stays
    assert {r.author for r in rows.values()} == {"agent"}
    assert rows[3].message == "candidate"
    # a delegation iterating on ITS OWN candidate moves the pointer the same
    # way (v3 published first: the agent's v3 candidate would be foreign)
    await green_run(env.sf, 3)
    out = json.loads(await env.tools["playbook_publish"](name="greeter", explanation=EXPLANATION))
    assert out.get("status") == "published", out
    did = uuid.uuid4()
    out = await _edit_as(env, did, PY_CODE_2)
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 4, out
    out = await _edit_as(env, did, PY_CODE_3)
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 5, out
    rows = await _rows(env.sf, "greeter")
    assert rows[4].author == rows[5].author == f"delegation:{did}"
    assert rows[5].message == "candidate"


@pytest.mark.asyncio
async def test_read_header_warns_before_write(env):
    did = await _foreign_candidate(env)
    read = await _read(env)
    assert read["candidate_author"] == f"delegation:{did}"
    assert read["conflict"]["candidate_version"] == 2
    assert read["conflict"]["author"] == f"delegation:{did}"
    assert read["instructions"].startswith("Another author's candidate exists — do not write; ask the owner")
    assert "replace_candidate=true" in read["instructions"]
    # no conflict for the author itself: no `conflict` key, author still shown
    token = _delegation_id.set(did)
    try:
        own = await _read(env)
    finally:
        _delegation_id.reset(token)
    assert own["candidate_author"] == f"delegation:{did}"
    assert "conflict" not in own
    assert not own["instructions"].startswith("Another author's")
    # no candidate at all: author None
    await _propose_py(env, name="fresh")
    await green_run(env.sf, 1, name="fresh")
    await env.tools["playbook_publish"](name="fresh", explanation=EXPLANATION)
    plain = await _read(env, name="fresh")
    assert plain["candidate_author"] is None and "conflict" not in plain


@pytest.mark.asyncio
async def test_replace_candidate_is_explicit(env):
    did = await _foreign_candidate(env)
    read = await _read(env)
    out = await _write(env, read["ticket"], PY_CODE_3, replace_candidate=True)
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 3
    rows = await _rows(env.sf, "greeter")
    assert rows[3].author == "agent"
    assert rows[3].message == f"candidate (replaced delegation:{did} v2 on owner instruction)"
    assert rows[2].code == PY_CODE_2  # the replaced row stays in history
    pb = await _playbook(env.sf, "greeter")
    assert pb.candidate_version == 3
    # the flag is a no-op when there is nothing foreign to replace
    read = await _read(env)
    out = await _write(env, read["ticket"], PY_CODE_2, replace_candidate=True)
    assert out["status"] == "candidate_saved"
    rows = await _rows(env.sf, "greeter")
    assert rows[4].message == "candidate"
    # and the ToolDef declares it
    defs = {td.name: td for td, _ in build_tools(env.sf, _Bus(), _StubRunner(), _Ctx(env.approvals))}
    prop = defs["playbook_edit"].parameters["properties"]["replace_candidate"]
    assert prop["type"] == "boolean" and "OWNER-authorised" in prop["description"]


@pytest.mark.asyncio
async def test_propose_recreate_refuses_foreign_candidate(env):
    await _propose_py(env)  # candidate v1 by the agent, never published
    async with env.sf() as s:
        pb = (await s.execute(select(Playbook).where(Playbook.name == "greeter"))).scalar_one()
        pb.status = "archived"
        await s.commit()
    did = uuid.uuid4()
    token = _delegation_id.set(did)
    try:
        out = await _propose_py(env, code=PY_CODE_2)
    finally:
        _delegation_id.reset(token)
    assert out["saved"] is False and out["stage"] == "write"
    assert "ticket" not in out and "ticket_still_valid" not in out
    assert "the agent" in out["error"] and "v1" in out["error"]
    assert out["conflict"]["candidate_version"] == 1
    assert out["conflict"]["author"] == "agent" and out["conflict"]["saved_at"]
    pb = await _playbook(env.sf, "greeter")
    assert pb.status == "archived" and pb.candidate_version == 1
    assert set(await _rows(env.sf, "greeter")) == {1}
    # the same author takes the name over as before (test_v2_parity's takeover)
    out = await _propose_py(env, code=PY_CODE_2)
    assert out["status"] == "candidate_saved" and out["candidate_version"] == 2
    pb = await _playbook(env.sf, "greeter")
    assert pb.status == "enabled"


# ---- the v2 prompt -----------------------------------------------------------

def _pb(fmt: str) -> Playbook:
    return Playbook(
        name="candidate-intake", display_name="candidate-intake",
        definition={"name": "candidate-intake", "steps": []} if fmt == "pblang"
        else {"name": "candidate-intake", "format": "python", "tools": []},
        code=PY_CODE if fmt == "python" else PB_CODE,
        format=fmt, status="enabled", manifest="INTENT: intake candidates",
    )


def _headers(p: str) -> list[str]:
    return re.findall(r"^## (\d+)\.", p, re.MULTILINE)


def test_v2_prompt_eleven_sections_in_order():
    want = [str(i) for i in range(1, 12)]
    assert _headers(_delegate_prompt("task", None)) == want
    assert _headers(_delegate_prompt("task", _pb("python"))) == want
    assert _headers(_delegate_prompt("task", _pb("pblang"))) == want
    assert _headers(_delegate_prompt("task", None, format="pblang")) == want


def test_v2_prompt_language_rules():
    p = _delegate_prompt("fix the phones", _pb("python"))
    pblang = _delegate_prompt("fix the phones", _pb("pblang"))
    assert p != pblang
    assert V2_PROMPT_MARKER in p and V2_PROMPT_MARKER not in pblang
    assert "async def run(ctx, inputs)" in p
    assert "Do not call playbook_validate" in p or "do not call playbook_validate" in p
    assert "playbook_dry_run" in p and "playbook_run_candidate" in p
    assert "stubs_from_run=" in p
    assert "status: parked" in p and "END your turn" in p
    assert V2_SKILL_BODY.rstrip() in p  # the v2 rules, verbatim
    assert "playbook_language_reference" not in p
    assert "collect=" not in p
    assert "3 failed writes" in p and "3 failed validates" not in p
    assert "validated: true" in p
    assert p.rstrip().endswith(_PROMPT_TAIL_V2)
    assert 3 <= len(_PROMPT_TAIL_V2.strip().splitlines()) <= 5
    assert _PROMPT_TAIL_V2.strip().splitlines()[-1].endswith(
        "The v2 rules above, not memory, are the source of the ctx.* contract.")
    assert not p.rstrip().endswith(_PROMPT_TAIL)
    # brief + manifest + target still carried
    assert "fix the phones" in p and "INTENT: intake candidates" in p
    assert "candidate-intake" in p
    # the checklist still sits right before the publish instruction
    assert p.index("Pre-publish checklist") < p.index("playbook_publish(name, explanation=")
    assert "the last write returned validated: true" in p.lower()
    # emphasis stays scarce
    shouty = [
        ln for ln in p.splitlines()
        if len(ln) > 8 and ln == ln.upper() and any(c.isalpha() for c in ln)
    ]
    assert len(shouty) <= 5, shouty
    assert len(p) <= len(pblang) + V2_SKILL_MAX_BYTES
    # a new playbook is python
    assert V2_PROMPT_MARKER in _delegate_prompt("build a digest", None)
    # explicit format wins over the target's column
    assert V2_PROMPT_MARKER in _delegate_prompt("task", _pb("pblang"), format="python")
    assert "playbook_language_reference" in _delegate_prompt("task", _pb("python"), format="pblang")


@pytest.mark.asyncio
async def test_prompt_follows_target_format(env):
    async with env.sf() as s:
        s.add(_pb("python"))
        s.add(Playbook(
            name="old-intake", display_name="old-intake",
            definition={"name": "old-intake", "steps": []}, code=PB_CODE,
            format="pblang", status="enabled",
        ))
        await s.commit()
    for name, marker, absent in (
        ("candidate-intake", V2_PROMPT_MARKER, "playbook_language_reference"),
        ("old-intake", "playbook_language_reference", V2_PROMPT_MARKER),
    ):
        agent = FakeAgent(result="done")
        by_name = {td.name: h for td, h in build_delegation_tools(FakeCtx(agent), env.sf, AUTHORING)}
        out = json.loads(await by_name["playbook_agent"](task="fix it", playbook=name, wait_seconds=5))
        await _wait_settled(env.sf, out["delegation_id"])
        prompt = agent.calls[0]["prompt"]
        assert marker in prompt and absent not in prompt, name
        assert _headers(prompt) == [str(i) for i in range(1, 12)]
    # from scratch → python
    agent = FakeAgent(result="done")
    by_name = {td.name: h for td, h in build_delegation_tools(FakeCtx(agent), env.sf, AUTHORING)}
    out = json.loads(await by_name["playbook_agent"](task="build a digest", wait_seconds=5))
    await _wait_settled(env.sf, out["delegation_id"])
    assert V2_PROMPT_MARKER in agent.calls[0]["prompt"]


# ---- steering text -----------------------------------------------------------

def test_delegation_skill_names_v2_loop():
    body = " ".join(_DELEGATION_SKILL_BODY.split())  # wrapped prose
    assert "validated on save" in body
    assert "real candidate run" in body
    assert "validate," not in body  # no separate validate step in the loop
    assert "publish when the candidate run is green" in body
    assert "test run is green" not in body
    skill = next(s for s in PlaybooksPlugin.manifest.skills if s.name == "playbook-delegation")
    assert skill.body == _DELEGATION_SKILL_BODY and len(skill.body) < 2560
    # the tool description says the same and names the format rule
    agent_def = next(
        td for td, _ in build_delegation_tools(FakeCtx(FakeAgent()), None, AUTHORING)
        if td.name == "playbook_agent"
    )
    desc = agent_def.description
    assert "validated on save" in desc and "real candidate run" in desc
    assert "New playbooks are written as python (v2); an edit follows the target's format." in desc
    assert "validate," not in desc
    example = agent_def.parameters["properties"]["task"]["description"]
    assert "publish when the candidate run is green" in example
