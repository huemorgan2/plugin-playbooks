"""0.9.0 (plans/002 phase 2, reshaped by 021) — manifest + staged edit flow.

playbook_edit is two-stage: read (manifest + code + single-use ticket) →
write (ticket required; compile → validate). 021: the manifest is context,
not law — the LLM drift gate and playbook_edit_force are gone, and
playbook_manifest_set is auto_approve.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from readstage import parse_read_stage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.agent_tools import _TICKET_TTL_SECONDS, build_tools
from plugin_playbooks.models import (
    Base,
    Playbook,
    PlaybookEditTicket,
    PlaybookVersion,
)


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _Agent:
    """run_llm stub — records calls, returns a fixed verdict or raises."""

    def __init__(self, result=None, exc=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = result if result is not None else {"conflict": False, "reason": ""}
        self.exc = exc

    async def run_llm(self, prompt, **kw):
        self.calls.append((prompt, kw))
        if self.exc:
            raise self.exc
        return self.result, {"total_tokens": 1}


class _Runner:
    _tools = None

    def __init__(self, agent=None):
        self._agent = agent


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    agent = _Agent()
    tools = {
        td.name: handler
        for td, handler in build_tools(sf, _Bus(), _Runner(agent))
    }
    yield sf, tools, agent
    await engine.dispose()


CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)
NEW_CODE = CODE.replace("inputs.greeting", "inputs.name")
MANIFEST = "## Purpose\nGreets the owner.\n## Never\nNever email anyone.\n"


async def _get(sf, name: str) -> Playbook:
    async with sf() as s:
        return (await s.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one()


async def _read_stage(tools, name: str = "greeter") -> dict:
    return parse_read_stage(await tools["playbook_edit"](name=name))


# --- read stage ---

@pytest.mark.asyncio
async def test_read_stage_returns_manifest_code_and_ticket(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE, manifest=MANIFEST)
    out = await _read_stage(tools)
    assert out["stage"] == "read"
    assert out["manifest"] == MANIFEST
    assert out["code"] == CODE
    assert out["version"] == 1
    assert out["expires_in_seconds"] == _TICKET_TTL_SECONDS
    assert out["ticket"]
    assert "manifest_note" not in out


@pytest.mark.asyncio
async def test_read_stage_flags_missing_manifest(env):
    _, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = await _read_stage(tools)
    assert out["manifest"] == ""
    assert "playbook_manifest_set" in out["manifest_note"]


# --- ticket gate ---

@pytest.mark.asyncio
async def test_write_without_ticket_is_refused(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_edit"](name="greeter", code=NEW_CODE))
    assert "ticket is required" in out["error"]
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket="not-a-uuid", code=NEW_CODE,
    ))
    assert "Invalid edit ticket" in out["error"]
    assert (await _get(sf, "greeter")).version == 1


@pytest.mark.asyncio
async def test_ticket_is_single_use(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    ticket = (await _read_stage(tools))["ticket"]
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=ticket, code=NEW_CODE,
    ))
    assert out["status"] == "candidate_saved", out
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=ticket, code=CODE,
    ))
    assert "already used" in out["error"]


@pytest.mark.asyncio
async def test_ticket_is_playbook_bound_and_expires(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    await tools["playbook_propose"](
        name="other", code=CODE.replace("greeter", "other"),
    )
    ticket = (await _read_stage(tools, "other"))["ticket"]
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=ticket, code=NEW_CODE,
    ))
    assert "Unknown edit ticket" in out["error"]

    ticket = (await _read_stage(tools))["ticket"]
    async with sf() as s:
        await s.execute(update(PlaybookEditTicket).values(
            created_at=datetime.now(timezone.utc)
            - timedelta(seconds=_TICKET_TTL_SECONDS + 60),
        ))
        await s.commit()
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=ticket, code=NEW_CODE,
    ))
    assert "expired" in out["error"]


@pytest.mark.asyncio
async def test_issue_sweeps_used_and_expired_tickets(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    ticket = (await _read_stage(tools))["ticket"]
    await tools["playbook_edit"](name="greeter", ticket=ticket, code=NEW_CODE)  # used
    await _read_stage(tools)  # issues a new one → sweeps the used row
    async with sf() as s:
        rows = (await s.execute(select(PlaybookEditTicket))).scalars().all()
    assert len(rows) == 1
    assert rows[0].used_at is None


@pytest.mark.asyncio
async def test_compile_error_does_not_burn_the_ticket(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    ticket = (await _read_stage(tools))["ticket"]
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=ticket,
        code="playbook(name='greeter')\nx = tool('t', v=nope)\n",
    ))
    assert "does not compile" in out["error"]
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=ticket, code=NEW_CODE,
    ))
    assert out["status"] == "candidate_saved", out


@pytest.mark.asyncio
async def test_concurrent_change_is_refused(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    ticket = (await _read_stage(tools))["ticket"]
    async with sf() as s:
        await s.execute(update(Playbook).values(version=7))
        await s.commit()
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=ticket, code=NEW_CODE,
    ))
    assert "changed while you were editing" in out["error"]


# --- no drift gate (021) ---

@pytest.mark.asyncio
async def test_edit_never_calls_the_llm_even_with_a_manifest(env):
    # 021: the manifest is context, not law — no drift judge on any edit.
    _, tools, agent = env
    await tools["playbook_propose"](name="greeter", code=CODE, manifest=MANIFEST)
    ticket = (await _read_stage(tools))["ticket"]
    out = json.loads(await tools["playbook_edit"](
        name="greeter", ticket=ticket, code=NEW_CODE,
    ))
    assert out["status"] == "candidate_saved"
    assert agent.calls == []


def test_edit_force_tool_is_gone(env):
    _, tools, _ = env
    assert "playbook_edit_force" not in tools


# --- manifest tool + snapshots ---

@pytest.mark.asyncio
async def test_manifest_set_snapshots_and_bumps(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await tools["playbook_manifest_set"](
        name="greeter", manifest=MANIFEST,
    ))
    assert out["status"] == "manifest_set"
    assert out["version"] == 2
    pb = await _get(sf, "greeter")
    assert pb.manifest == MANIFEST
    assert pb.live_version == 2  # the manifest is live content
    async with sf() as s:
        vers = {v.version: v for v in (await s.execute(select(PlaybookVersion))).scalars().all()}
    assert set(vers) == {1, 2}
    assert vers[1].manifest == ""          # pre-change live recorded
    assert vers[2].manifest == MANIFEST    # new live version carries it
    assert vers[2].message == "manifest updated"


@pytest.mark.asyncio
async def test_edit_snapshot_carries_manifest(env):
    sf, tools, _ = env
    await tools["playbook_propose"](name="greeter", code=CODE, manifest=MANIFEST)
    ticket = (await _read_stage(tools))["ticket"]
    await tools["playbook_edit"](name="greeter", ticket=ticket, code=NEW_CODE)
    async with sf() as s:
        vers = (await s.execute(select(PlaybookVersion))).scalars().all()
    assert vers and all(v.manifest == MANIFEST for v in vers)


# --- policies ---

def test_approval_policies():
    tds = {td.name: td for td, _ in build_tools(None, _Bus(), _Runner())}
    # 021: the manifest is context, not law — keeping it fresh must be cheap.
    assert tds["playbook_manifest_set"].policy == "auto_approve"
    assert getattr(tds["playbook_edit"], "policy", None) != "prompt_always"
    assert "ticket" in tds["playbook_edit"].parameters["properties"]
    assert "manifest" in tds["playbook_propose"].parameters["properties"]
