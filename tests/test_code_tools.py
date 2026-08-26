"""0.8.0 (plans/002 phase 1) — code-first authoring tools.

playbook_propose/edit accept `code` (the pblang source); edit also takes an
old=/new= snippet diff; get_definition returns code by default; the on-load
backfill stores verified codegen for pre-code playbooks.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import backfill_code
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookVersion


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _Runner:
    _tools = None


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    tools = {
        td.name: handler for td, handler in build_tools(sf, _Bus(), _Runner())
    }
    yield sf, tools
    await engine.dispose()


CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)


async def _get(sf, name: str) -> Playbook:
    async with sf() as s:
        return (await s.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one()


async def _ticket(tools, name: str) -> str:
    """0.9.0: the write stage of playbook_edit needs a read-stage ticket."""
    out = json.loads(await tools["playbook_edit"](name=name))
    return out["ticket"]


@pytest.mark.asyncio
async def test_propose_with_code_stores_code_and_definition(env):
    sf, tools = env
    out = json.loads(await tools["playbook_propose"](name="greeter", code=CODE))
    assert out["status"] == "created", out
    pb = await _get(sf, "greeter")
    assert pb.code == CODE
    step = pb.definition["steps"][0]
    assert step["id"] == "say"
    assert step["kind"] == "tool_call"
    assert step["tool"] == "send_chat_message"
    assert step["args"] == {"message": "{{ inputs.greeting }}"}


@pytest.mark.asyncio
async def test_propose_requires_exactly_one_format(env):
    _, tools = env
    out = json.loads(await tools["playbook_propose"](name="x"))
    assert "exactly one" in out["error"]
    out = json.loads(await tools["playbook_propose"](
        name="x", code=CODE, definition_yaml="name: x\nsteps: []",
    ))
    assert "exactly one" in out["error"]


@pytest.mark.asyncio
async def test_propose_compile_errors_are_reported_with_lines(env):
    _, tools = env
    out = json.loads(await tools["playbook_propose"](
        name="bad", code="playbook(name='bad')\nx = tool('t', v=nope)\n",
    ))
    assert "does not compile" in out["error"]
    assert out["issues"][0]["line"] == 2


@pytest.mark.asyncio
async def test_code_cannot_rename(env):
    sf, tools = env
    code = "playbook(name='other-name')\nsay = tool('t', m='x')\n"
    out = json.loads(await tools["playbook_propose"](name="pinned", code=code))
    assert out["status"] == "created"
    pb = await _get(sf, "pinned")
    assert pb.definition["name"] == "pinned"


@pytest.mark.asyncio
async def test_get_definition_returns_code_by_default_and_yaml_on_request(env):
    _, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    got = await tools["playbook_get_definition"](name="greeter")
    assert got == CODE
    yaml_out = await tools["playbook_get_definition"](name="greeter", format="yaml")
    assert "kind: tool_call" in yaml_out


@pytest.mark.asyncio
async def test_get_definition_derives_code_for_yaml_playbooks(env):
    sf, tools = env
    async with sf() as s:
        s.add(Playbook(
            name="legacy", display_name="legacy",
            definition={"name": "legacy", "steps": [
                {"id": "a", "kind": "tool_call", "tool": "t", "args": {}},
            ]},
            status="enabled",
        ))
        await s.commit()
    got = await tools["playbook_get_definition"](name="legacy")
    assert "a = tool('t')" in got


@pytest.mark.asyncio
async def test_edit_with_code_saves_a_candidate(env):
    # 0.10.0: a save creates a CANDIDATE version row — live is untouched
    # until playbook_promote.
    sf, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    new_code = CODE.replace("inputs.greeting", "inputs.name")
    out = json.loads(await tools["playbook_edit"](name="greeter", ticket=await _ticket(tools, "greeter"), code=new_code))
    assert out["status"] == "candidate_saved"
    assert out["candidate_version"] == 2
    assert out["live_version"] == 1
    pb = await _get(sf, "greeter")
    assert pb.code == CODE  # live untouched
    assert pb.candidate_version == 2
    async with sf() as s:
        vers = {v.version: v for v in (await s.execute(select(PlaybookVersion))).scalars().all()}
    assert set(vers) == {1, 2}
    assert vers[1].code == CODE       # live content recorded
    assert vers[2].code == new_code   # the candidate holds the NEW content
    assert vers[2].definition["steps"][0]["args"] == {"message": "{{ inputs.name }}"}


@pytest.mark.asyncio
async def test_snippet_edit_unique_match(env):
    sf, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=await _ticket(tools, "greeter"),
        old="inputs.greeting", new="inputs.name",
    ))
    assert out["status"] == "candidate_saved", out
    async with sf() as s:
        cand = (await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.version == out["candidate_version"])
        )).scalar_one()
    assert "inputs.name" in cand.code


@pytest.mark.asyncio
async def test_snippet_edit_rejects_missing_and_ambiguous(env):
    _, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=await _ticket(tools, "greeter"),
        old="not in the code", new="x",
    ))
    assert "not found" in out["error"]
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=await _ticket(tools, "greeter"),
        old="in", new="x",   # matches many places
    ))
    assert "matches" in out["error"]


@pytest.mark.asyncio
async def test_snippet_edit_that_breaks_compile_is_rejected(env):
    sf, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=await _ticket(tools, "greeter"),
        old="inputs.greeting", new="mystery.field",
    ))
    assert "does not compile" in out["error"]
    pb = await _get(sf, "greeter")
    assert pb.version == 1  # nothing was saved
    assert pb.code == CODE


@pytest.mark.asyncio
async def test_edit_requires_exactly_one_mode(env):
    _, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_edit"](name="greeter"))
    assert out["stage"] == "read"  # no payload = the read stage, not an error
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=out["ticket"], code=CODE, old="a", new="b",
    ))
    assert "exactly one" in out["error"]


@pytest.mark.asyncio
async def test_yaml_edit_regenerates_code(env):
    sf, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    yaml_src = (
        "name: greeter\n"
        "steps:\n"
        "  - id: say\n"
        "    kind: tool_call\n"
        "    tool: send_chat_message\n"
        "    args: {message: 'hello'}\n"
    )
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=await _ticket(tools, "greeter"),
        definition_yaml=yaml_src,
    ))
    assert out["status"] == "candidate_saved"
    async with sf() as s:
        cand = (await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.version == out["candidate_version"])
        )).scalar_one()
    assert "message='hello'" in cand.code  # regenerated, not the stale source


@pytest.mark.asyncio
async def test_validate_accepts_code(env):
    _, tools = env
    out = json.loads(await tools["playbook_validate"](code=CODE))
    assert out["ok"] is True
    assert out["saved"] is False
    out = json.loads(await tools["playbook_validate"](
        code="playbook(name='b')\nx = tool('t', v=nope)\n",
    ))
    assert out["ok"] is False
    assert out["errors"][0]["line"] == 2


@pytest.mark.asyncio
async def test_backfill_fills_missing_code_only(env):
    sf, tools = env
    await tools["playbook_propose"](name="greeter", code=CODE)  # already has code
    async with sf() as s:
        s.add(Playbook(
            name="legacy", display_name="legacy",
            definition={"name": "legacy", "steps": [
                {"id": "a", "kind": "tool_call", "tool": "t", "args": {}},
            ]},
            status="enabled",
        ))
        await s.commit()
    filled = await backfill_code(sf)
    assert filled == 1
    legacy = await _get(sf, "legacy")
    assert "a = tool('t')" in legacy.code
    greeter = await _get(sf, "greeter")
    assert greeter.code == CODE  # untouched
    assert await backfill_code(sf) == 0  # idempotent
