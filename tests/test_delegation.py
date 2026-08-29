"""plans/013 phase 1 — playbook delegation core.

Pins the delegation contract: playbook_agent creates a row and drives ONE
contained ctx.agent.run_turn (explicit allowlist, max_turns=40, timeout,
no memory writes); the main-turn payload stays ~1KB; the event feed maps
duck-typed pydantic-ai events onto the owner-word phase vocabulary; every
terminal path (done / _aborted → needs_owner / error → failed / crash →
failed) lands on the row.
"""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — installs the luna_sdk stub via conftest
from plugin_playbooks import PlaybooksPlugin
from plugin_playbooks.delegation import (
    _TASKS,
    build_delegation_tools,
    current_phase,
    delegate_toolset,
    phase_for_tool,
    sweep_orphaned_delegations,
)
from plugin_playbooks.models import Base, Playbook, PlaybookDelegation

AUTHORING = PlaybooksPlugin.AUTHORING_TOOLS


# ---- fakes -------------------------------------------------------------------


class _ToolCallPart:
    def __init__(self, tool_name: str, tool_call_id: str) -> None:
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id


class FunctionToolCallEvent:  # duck-typed by NAME in delegation.py
    def __init__(self, tool_name: str, call_id: str) -> None:
        self.part = _ToolCallPart(tool_name, call_id)


class _ToolReturnPart:
    def __init__(self, tool_name: str, tool_call_id: str, content) -> None:
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.content = content


class FunctionToolResultEvent:
    """pydantic-ai <2 shape: the ToolReturnPart rides on .result."""

    def __init__(self, tool_name: str, call_id: str, content) -> None:
        self.result = _ToolReturnPart(tool_name, call_id, content)


class _FunctionToolResultEventV2:
    """pydantic-ai >=2 shape (QA runs 2.35): the part rides on .part,
    .content is the model-facing echo, and there is no .result at all."""

    def __init__(self, tool_name: str, call_id: str, content) -> None:
        self.part = _ToolReturnPart(tool_name, call_id, content)
        self.content = content


_FunctionToolResultEventV2.__name__ = "FunctionToolResultEvent"
_FunctionToolResultEventV2.__qualname__ = "FunctionToolResultEvent"


class _TextPart:
    def __init__(self, content: str) -> None:
        self.content = content


class PartStartEvent:
    def __init__(self, content: str) -> None:
        self.part = _TextPart(content)


class FakeAgent:
    """Scripted ctx.agent — records run_turn kwargs, plays an event script,
    then returns a canned result."""

    def __init__(self, result="All done: candidate v3 promoted.",
                 events=None, gate: asyncio.Event | None = None,
                 reject_049_kwargs: bool = False,
                 raise_exc: Exception | None = None) -> None:
        self.result = result
        self.script = events or []
        self.gate = gate
        self.reject_049_kwargs = reject_049_kwargs
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def run_turn(self, prompt, **kwargs):
        if self.reject_049_kwargs and (
            "max_turns" in kwargs or "event_stream_handler" in kwargs
        ):
            raise TypeError("run_turn() got an unexpected keyword argument")
        self.calls.append({"prompt": prompt, **kwargs})
        handler = kwargs.get("event_stream_handler")
        if handler is not None and self.script:
            async def _stream():
                for ev in self.script:
                    yield ev
            await handler(None, _stream())
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result, {"total_tokens": 1234}


class FakeCtx:
    def __init__(self, agent: FakeAgent) -> None:
        self.agent = agent
        self.current_conversation_id = uuid.uuid4()


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    for t in list(_TASKS.values()):
        t.cancel()
    await asyncio.sleep(0)
    _TASKS.clear()
    await engine.dispose()


def _tools(ctx, sf):
    pairs = build_delegation_tools(ctx, sf, AUTHORING)
    by_name = {td.name: (td, h) for td, h in pairs}
    return by_name


async def _wait_settled(sf, delegation_id, timeout=20.0):
    async def _poll():
        while True:
            async with sf() as s:
                row = await s.get(PlaybookDelegation, uuid.UUID(delegation_id))
            if row is not None and row.status != "running":
                return row
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(_poll(), timeout)


# ---- the contract ------------------------------------------------------------


async def test_fast_path_returns_done_inline(env):
    agent = FakeAgent(result="Fixed the phone step; 8/8 specs pass; promoted v4.")
    tools = _tools(FakeCtx(agent), env)
    _, handler = tools["playbook_agent"]

    out = json.loads(await handler(task="fix phones", wait_seconds=10))
    assert out["status"] == "done"
    assert "promoted" in out["report"]
    # ONE call, contained: explicit allowlist + step budget + no memory writes.
    call = agent.calls[0]
    assert call["max_turns"] == 40
    assert call["memory_write"] is False
    assert call["timeout_s"] == 900.0
    assert "playbook_edit" in call["tools"]
    assert "send_chat_message" not in call["tools"]
    # A row exists and matches.
    async with env() as s:
        row = (await s.execute(select(PlaybookDelegation))).scalar_one()
    assert row.status == "done"
    assert row.card_token  # phase-2 capability token minted at creation


async def test_slow_path_returns_running_then_status_polls_done(env):
    gate = asyncio.Event()
    agent = FakeAgent(result="done late", gate=gate)
    tools = _tools(FakeCtx(agent), env)
    _, run = tools["playbook_agent"]
    _, status = tools["playbook_agent_status"]

    out = json.loads(await run(task="slow job", wait_seconds=0.05))
    assert out["status"] == "running"
    # The running payload steers the main agent to END ITS TURN, not poll.
    assert "END YOUR TURN" in out["message"]

    st = json.loads(await status(delegation_id=out["delegation_id"]))
    assert st["status"] == "running"

    gate.set()
    row = await _wait_settled(env, out["delegation_id"])
    assert row.status == "done"
    st = json.loads(await status(delegation_id=out["delegation_id"]))
    assert st["status"] == "done"
    assert st["report"] == "done late"


async def test_aborted_maps_to_needs_owner(env):
    agent = FakeAgent(
        result={"_aborted": "usage limit exceeded: request_limit=40"},
        events=[FunctionToolCallEvent("playbook_edit", "c1"),
                FunctionToolResultEvent("playbook_edit", "c1", "candidate_saved")],
    )
    tools = _tools(FakeCtx(agent), env)
    _, run = tools["playbook_agent"]
    out = json.loads(await run(task="huge job", wait_seconds=10))
    assert out["status"] == "needs_owner"
    assert "step budget" in out["report"]
    assert "playbook_edit" in out["report"]  # says where it stopped


async def test_error_result_and_crash_map_to_failed(env):
    agent = FakeAgent(result={"_aborted": None, "error": "model unavailable"})
    tools = _tools(FakeCtx(agent), env)
    _, run = tools["playbook_agent"]
    out = json.loads(await run(task="job", wait_seconds=10))
    assert out["status"] == "failed"
    assert "model unavailable" in out["report"]

    agent2 = FakeAgent(raise_exc=RuntimeError("boom"))
    tools2 = _tools(FakeCtx(agent2), env)
    _, run2 = tools2["playbook_agent"]
    out2 = json.loads(await run2(task="job2", wait_seconds=10))
    assert out2["status"] == "failed"
    assert "boom" in out2["report"]


async def test_toolset_includes_referenced_tools_of_target_playbook(env):
    async with env() as s:
        s.add(Playbook(
            name="candidate-intake",
            display_name="candidate-intake",
            definition={"name": "candidate-intake", "steps": [
                {"id": "a", "kind": "tool_call", "tool": "gmail_search"},
                {"id": "b", "kind": "condition", "when": "true",
                 "then": [{"id": "c", "kind": "tool_call",
                           "tool": "sheets_append"}],
                 "else": [{"id": "d", "kind": "tool_call",
                           "tool": "send_chat_message"}]},
                {"id": "e", "kind": "loop", "over": "{{ [1] }}",
                 "body": [{"id": "f", "kind": "tool_call",
                           "tool": "slack_post"}]},
            ]},
            status="enabled",
            manifest="INTENT: intake candidates",
        ))
        await s.commit()

    tools = await delegate_toolset(env, "candidate-intake", AUTHORING)
    for expected in ("gmail_search", "sheets_append", "slack_post",
                     "playbook_list", "playbook_status", "playbook_publish"):
        assert expected in tools
    assert "send_chat_message" not in tools  # card is the surface, always

    # The delegate prompt carries the manifest of the target playbook.
    agent = FakeAgent()
    pairs = _tools(FakeCtx(agent), env)
    _, run = pairs["playbook_agent"]
    out = json.loads(await run(task="fix it", playbook="candidate-intake",
                               wait_seconds=10))
    assert out["status"] == "done"
    assert "INTENT: intake candidates" in agent.calls[0]["prompt"]
    assert "gmail_search" in agent.calls[0]["tools"]


async def test_unknown_playbook_is_a_clean_error(env):
    tools = _tools(FakeCtx(FakeAgent()), env)
    _, run = tools["playbook_agent"]
    out = json.loads(await run(task="fix it", playbook="nope"))
    assert "not found" in out["error"]
    async with env() as s:
        assert (await s.execute(select(PlaybookDelegation))).first() is None


async def test_events_recorded_with_inferred_phases(env):
    agent = FakeAgent(
        result="ok",
        events=[
            FunctionToolCallEvent("playbook_get_definition", "c1"),
            FunctionToolResultEvent("playbook_get_definition", "c1", "{...}"),
            PartStartEvent("The bug is in the phone step.\nMore detail."),
            FunctionToolCallEvent("playbook_edit", "c2"),
            FunctionToolResultEvent("playbook_edit", "c2", "candidate_saved"),
            FunctionToolCallEvent("playbook_spec_run", "c3"),
            FunctionToolResultEvent("playbook_spec_run", "c3", "8/8 pass"),
            FunctionToolCallEvent("playbook_publish", "c4"),
            FunctionToolResultEvent("playbook_publish", "c4", "v4 live"),
        ],
    )
    tools = _tools(FakeCtx(agent), env)
    _, run = tools["playbook_agent"]
    out = json.loads(await run(task="fix", wait_seconds=10))

    async with env() as s:
        row = await s.get(PlaybookDelegation, uuid.UUID(out["delegation_id"]))
    phases = [e["phase"] for e in row.events if e["kind"] == "tool"]
    assert phases == ["Understand", "Change", "Prove", "Ship"]
    assert row.steps_used == 4
    # Call + result collapse into ONE line per call, result detail attached.
    edits = [e for e in row.events if e["label"] == "playbook_edit"]
    assert len(edits) == 1
    assert edits[0]["detail"] == "candidate_saved"
    assert edits[0]["ms"] is not None
    # The delegate's inter-tool text lands as a one-line thought.
    thoughts = [e for e in row.events if e["kind"] == "thought"]
    assert thoughts[0]["label"] == "The bug is in the phone step."
    assert current_phase(row.events) == "Ship"
    # Status tool surfaces a small recent-events tail.
    _, status = tools["playbook_agent_status"]
    st = json.loads(await status(delegation_id=out["delegation_id"]))
    assert 0 < len(st["recent_events"]) <= 5


async def test_v2_result_events_still_stamp_duration_and_detail(env):
    # QA's pydantic-ai 2.35 sends results as .part/.content with no .result
    # — the shape that shipped ms=None to the live card until this test.
    agent = FakeAgent(
        result="ok",
        events=[
            FunctionToolCallEvent("playbook_spec_run", "c1"),
            _FunctionToolResultEventV2("playbook_spec_run", "c1", "2/2 pass"),
        ],
    )
    tools = _tools(FakeCtx(agent), env)
    _, run = tools["playbook_agent"]
    out = json.loads(await run(task="check", wait_seconds=10))
    async with env() as s:
        row = await s.get(PlaybookDelegation, uuid.UUID(out["delegation_id"]))
    runs = [e for e in row.events if e["label"] == "playbook_spec_run"]
    assert len(runs) == 1  # call + result still collapse to one line
    assert runs[0]["ms"] is not None
    assert runs[0]["detail"] == "2/2 pass"


async def test_typeerror_fallback_runs_uncontained_and_says_so(env):
    agent = FakeAgent(result="done anyway", reject_049_kwargs=True)
    tools = _tools(FakeCtx(agent), env)
    _, run = tools["playbook_agent"]
    out = json.loads(await run(task="job", wait_seconds=10))
    assert out["status"] == "done"
    # The fallback call carries no budget kwargs but keeps the allowlist.
    call = agent.calls[0]
    assert "max_turns" not in call
    assert "playbook_edit" in call["tools"]
    async with env() as s:
        row = await s.get(PlaybookDelegation, uuid.UUID(out["delegation_id"]))
    assert any("budget unenforced" in e["label"] for e in row.events)


async def test_main_turn_payloads_stay_small(env):
    gate = asyncio.Event()
    agent = FakeAgent(result="x" * 5000, gate=gate)
    tools = _tools(FakeCtx(agent), env)
    _, run = tools["playbook_agent"]
    running = await run(task="job", wait_seconds=0.05)
    assert len(running) < 1024

    gate.set()
    out = json.loads(running)
    await _wait_settled(env, out["delegation_id"])
    _, status = tools["playbook_agent_status"]
    done = await status(delegation_id=out["delegation_id"])
    assert len(done) < 1200  # report capped at 800 chars + envelope


async def test_empty_task_rejected(env):
    tools = _tools(FakeCtx(FakeAgent()), env)
    _, run = tools["playbook_agent"]
    out = json.loads(await run(task="   "))
    assert out["error"]


async def test_orphan_sweep_fails_rows_without_live_tasks(env):
    async with env() as s:
        s.add(PlaybookDelegation(task="zombie", status="running",
                                 card_token="t" * 10))
        await s.commit()
    swept = await sweep_orphaned_delegations(env)
    assert swept == 1
    async with env() as s:
        row = (await s.execute(select(PlaybookDelegation))).scalar_one()
    assert row.status == "failed"
    assert "restarted" in row.result


def test_phase_vocabulary_is_owner_words_only():
    assert phase_for_tool("playbook_edit") == "Change"
    assert phase_for_tool("playbook_dry_run") == "Prove"
    assert phase_for_tool("playbook_publish") == "Ship"
    assert phase_for_tool("gmail_search") == "Understand"  # probing = exploring
    for t in AUTHORING:
        assert phase_for_tool(t) in ("Understand", "Change", "Prove", "Ship")


def test_delegation_tools_are_skill_gated_and_chat_only(env):
    pairs = build_delegation_tools(FakeCtx(FakeAgent()), env, AUTHORING)
    by_name = {td.name: td for td, _ in pairs}
    assert set(by_name) == {"playbook_agent", "playbook_agent_status"}
    assert by_name["playbook_agent"].chat_only is True  # recursion guard
    assert set(PlaybooksPlugin.DELEGATION_TOOLS) == set(by_name)
    skill = next(s for s in PlaybooksPlugin.manifest.skills
                 if s.name == "playbook-delegation")
    assert set(skill.tools) == set(by_name)
    assert len(skill.body) < 2048  # the point: a SMALL unlock, not 12KB


def test_skill_descriptions_steer_playbook_jobs_to_delegation():
    # plans/013 phase 4, found in the dojo: "fix the playbook and make it
    # live" loaded playbook-authoring and ran the whole loop in the main
    # context — the old authoring description claimed EVERY create/modify
    # job ("load before creating or modifying any playbook"), beating the
    # delegation skill at its own scenario. The steering contract:
    # authoring presents itself as the inline / build-it-together path and
    # points at delegation; delegation presents itself as the default.
    skills = {s.name: s for s in PlaybooksPlugin.manifest.skills}
    auth = skills["playbook-authoring"].description
    deleg = skills["playbook-delegation"].description
    assert "inline" in auth.lower()
    assert "playbook-delegation" in auth
    assert "default" in deleg.lower()
    assert "any playbook" not in auth  # the phrasing that caused the miss
