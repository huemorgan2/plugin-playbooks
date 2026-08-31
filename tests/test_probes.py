"""0.12.0 (plans/002 phase 5) — preflight probes.

collect_tools/collect_subtasks over nested IR, the probe_tool matrix,
run_preflight caching, the probes promote gate, the playbook_preflight
tool, and daily-sweep transition detection (reprobe_enabled).
"""

from __future__ import annotations

import json

import pytest

from evidence import EXPLANATION, green_run
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookProbeResult
from plugin_playbooks.probes import (
    collect_subtasks,
    collect_tools,
    preflight_note,
    probe_tool,
    reprobe_enabled,
    run_preflight,
)
from plugin_playbooks.runner import PlaybookRunner


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _Probe:
    def __init__(self, handler=None, args=None) -> None:
        self.kind = "auth"
        self.handler = handler
        self.args = args


class _Def:
    def __init__(self, probe=None, policy="prompt") -> None:
        self.probe = probe
        self._policy = policy

    def effective_policy(self) -> str:
        return self._policy


class _Tool:
    def __init__(self, handler, definition=None, plugin="p") -> None:
        self.handler = handler
        self.definition = definition
        self.plugin = plugin


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
        tool_registry=_Tools(send_chat_message=_Tool(_noop), t=_Tool(_noop)),
        events=_Bus(),
    )
    tools = {
        td.name: (td, handler) for td, handler in build_tools(sf, _Bus(), runner)
    }
    handlers = {k: v[1] for k, v in tools.items()}
    yield sf, runner, handlers, tools
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


async def _ticket(handlers, name: str) -> str:
    out = parse_read_stage(await handlers["playbook_edit"](name=name))
    return out["ticket"]


# ---- collectors ------------------------------------------------------------

def test_collect_tools_walks_nested_ir():
    definition = {"steps": [
        {"id": "a", "kind": "tool_call", "tool": "t1"},
        {"id": "b", "kind": "condition",
         "then": [{"id": "c", "kind": "tool_call", "tool": "t2"}],
         "else": [{"id": "d", "kind": "loop",
                   "body": [{"id": "e", "kind": "tool_call", "tool": "t3"}]}]},
        {"id": "f", "kind": "parallel", "branches": [
            [{"id": "g", "kind": "tool_call", "tool": "t1"}],  # dupe
            [{"id": "h", "kind": "agent", "tools": ["t4", "t5"]}],
        ]},
    ]}
    assert collect_tools(definition) == ["t1", "t2", "t3", "t4", "t5"]
    assert collect_tools({}) == []


def test_collect_subtasks_walks_nested_ir():
    definition = {"steps": [
        {"id": "a", "kind": "subtask", "playbook": "child"},
        {"id": "b", "kind": "loop", "body": [
            {"id": "c", "kind": "subtask", "playbook": "other"},
        ]},
        {"id": "d", "kind": "tool_call", "tool": "t1"},
    ]}
    assert collect_subtasks(definition) == ["child", "other"]


# ---- probe_tool matrix -----------------------------------------------------

@pytest.mark.asyncio
async def test_probe_no_registry_is_unprobeable():
    res = await probe_tool(None, "x")
    assert res["status"] == "unprobeable"
    assert "no tool registry" in res["detail"]


@pytest.mark.asyncio
async def test_probe_missing_tool_fails_tool_missing():
    res = await probe_tool(_Tools(), "gone")
    assert res["status"] == "failed"
    assert res["failure_class"] == "tool_missing"


@pytest.mark.asyncio
async def test_probe_blocked_policy_fails_blocked():
    reg = _Tools(t=_Tool(_noop, _Def(policy="block")))
    res = await probe_tool(reg, "t")
    assert res["status"] == "failed"
    assert res["failure_class"] == "blocked"


@pytest.mark.asyncio
async def test_probe_absent_probe_is_unprobeable():
    reg = _Tools(t=_Tool(_noop, _Def(probe=None)))
    res = await probe_tool(reg, "t")
    assert res["status"] == "unprobeable"
    assert res["failure_class"] is None
    # a definition with no `probe` attribute at all (older core) is the same
    reg2 = _Tools(t=_Tool(_noop, object()))
    res2 = await probe_tool(reg2, "t")
    assert res2["status"] == "unprobeable"


@pytest.mark.asyncio
async def test_probe_handler_ok():
    async def probe():
        return {"ok": True, "detail": "db reachable"}
    reg = _Tools(t=_Tool(_noop, _Def(probe=_Probe(handler=probe))))
    res = await probe_tool(reg, "t")
    assert res["status"] == "ok"
    assert res["detail"] == "db reachable"


@pytest.mark.asyncio
async def test_probe_handler_failure_keeps_valid_class():
    async def probe():
        return {"ok": False, "failure_class": "credential_dead",
                "detail": "401 from upstream"}
    reg = _Tools(t=_Tool(_noop, _Def(probe=_Probe(handler=probe))))
    res = await probe_tool(reg, "t")
    assert res["status"] == "failed"
    assert res["failure_class"] == "credential_dead"
    assert res["detail"] == "401 from upstream"


@pytest.mark.asyncio
async def test_probe_handler_bogus_class_becomes_unknown():
    async def probe():
        return {"ok": False, "failure_class": "made_up"}
    reg = _Tools(t=_Tool(_noop, _Def(probe=_Probe(handler=probe))))
    res = await probe_tool(reg, "t")
    assert res["failure_class"] == "unknown"


@pytest.mark.asyncio
async def test_probe_args_mode_calls_tool_handler():
    seen = {}

    async def handler(**kw):
        seen.update(kw)
        return {"ok": True}
    reg = _Tools(t=_Tool(handler, _Def(probe=_Probe(args={"q": "ping"}))))
    res = await probe_tool(reg, "t")
    assert res["status"] == "ok"
    assert seen == {"q": "ping"}


@pytest.mark.asyncio
async def test_probe_handler_wins_over_args():
    async def probe():
        return {"ok": True, "detail": "via probe handler"}

    async def handler(**_kw):
        raise AssertionError("tool handler must not be called")
    reg = _Tools(t=_Tool(
        handler, _Def(probe=_Probe(handler=probe, args={"q": "x"}))
    ))
    res = await probe_tool(reg, "t")
    assert res["status"] == "ok"
    assert res["detail"] == "via probe handler"


@pytest.mark.asyncio
async def test_probe_with_neither_handler_nor_args_is_unprobeable():
    reg = _Tools(t=_Tool(_noop, _Def(probe=_Probe())))
    res = await probe_tool(reg, "t")
    assert res["status"] == "unprobeable"
    assert "neither" in res["detail"]


@pytest.mark.asyncio
async def test_raising_probe_fails_unknown():
    async def probe():
        raise RuntimeError("boom")
    reg = _Tools(t=_Tool(_noop, _Def(probe=_Probe(handler=probe))))
    res = await probe_tool(reg, "t")
    assert res["status"] == "failed"
    assert res["failure_class"] == "unknown"
    assert "boom" in res["detail"]


@pytest.mark.asyncio
async def test_non_dict_probe_output_counts_as_ok():
    async def probe():
        return "fine"
    reg = _Tools(t=_Tool(_noop, _Def(probe=_Probe(handler=probe))))
    res = await probe_tool(reg, "t")
    assert res["status"] == "ok"


# ---- run_preflight cache ---------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_upserts_and_drops_stale_rows(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    pb = await _get(sf, "greeter")

    reg = _Tools(send_chat_message=_Tool(_noop), t=_Tool(_noop))
    async with sf() as s:
        pb_row = (await s.execute(
            select(Playbook).where(Playbook.name == "greeter")
        )).scalar_one()
        summary = await run_preflight(s, reg, pb_row, pb_row.definition)
        await s.commit()
    assert summary == {
        "total": 1, "ok": 0, "unprobeable": 1, "failed": 0,
        "results": summary["results"],
    }
    async with sf() as s:
        rows = (await s.execute(
            select(PlaybookProbeResult).where(
                PlaybookProbeResult.playbook_id == pb.id
            )
        )).scalars().all()
    assert [(r.tool, r.status) for r in rows] == [
        ("send_chat_message", "unprobeable"),
    ]

    # re-run against a definition that uses a different tool: the old row
    # is dropped, the new one appears, and a failure is recorded.
    async with sf() as s:
        pb_row = (await s.execute(
            select(Playbook).where(Playbook.name == "greeter")
        )).scalar_one()
        await run_preflight(s, reg, pb_row, {"steps": [
            {"id": "a", "kind": "tool_call", "tool": "nope"},
        ]})
        await s.commit()
    async with sf() as s:
        rows = (await s.execute(
            select(PlaybookProbeResult).where(
                PlaybookProbeResult.playbook_id == pb.id
            )
        )).scalars().all()
    assert [(r.tool, r.status, r.failure_class) for r in rows] == [
        ("nope", "failed", "tool_missing"),
    ]


@pytest.mark.asyncio
async def test_preflight_follows_subtask_targets(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="child", code=(
        "playbook(name='child', description='c')\n"
        "go = tool('t', x=1)\n"
    ))
    await handlers["playbook_propose"](name="greeter", code=CODE)
    async with sf() as s:
        pb_row = (await s.execute(
            select(Playbook).where(Playbook.name == "greeter")
        )).scalar_one()
        summary = await run_preflight(s, _Tools(), pb_row, {"steps": [
            {"id": "a", "kind": "subtask", "playbook": "child"},
            {"id": "b", "kind": "tool_call", "tool": "send_chat_message"},
        ]})
        await s.commit()
    # child's tool `t` was probed alongside the direct tool
    assert sorted(r["tool"] for r in summary["results"]) == [
        "send_chat_message", "t",
    ]


# ---- the promote gate ------------------------------------------------------

@pytest.mark.asyncio
async def test_promote_passes_with_unprobeable_tools_and_notes_it(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers, "greeter"),
        old="says hi", new="says hello",
    )
    await green_run(sf, 2)
    out = json.loads(await handlers["playbook_publish"](explanation=EXPLANATION, name="greeter"))
    assert out["status"] == "published"
    gates = {g["gate"]: g for g in out["gates"]}
    assert gates["probes"]["ok"] is True
    assert gates["probes"]["note"] == "1 unprobeable"


@pytest.mark.asyncio
async def test_promote_refused_on_failed_probe_then_passes(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers, "greeter"),
        old="says hi", new="says hello",
    )

    async def dead():
        return {"ok": False, "failure_class": "credential_dead",
                "detail": "token expired"}
    tool = runner._tools._tools["send_chat_message"]
    tool.definition = _Def(probe=_Probe(handler=dead))

    await green_run(sf, 2)
    refused = json.loads(await handlers["playbook_publish"](explanation=EXPLANATION, name="greeter"))
    assert refused["gate"] == "probes"
    assert refused["failing_tools"][0]["tool"] == "send_chat_message"
    assert refused["failing_tools"][0]["failure_class"] == "credential_dead"
    pb = await _get(sf, "greeter")
    assert pb.candidate_version == 2  # candidate intact
    # the refusal cached the failure for the trust surface
    async with sf() as s:
        row = (await s.execute(
            select(PlaybookProbeResult).where(
                PlaybookProbeResult.playbook_id == pb.id
            )
        )).scalar_one()
    assert (row.tool, row.status) == ("send_chat_message", "failed")

    # connection fixed → gate passes with an ok count
    async def alive():
        return {"ok": True}
    tool.definition = _Def(probe=_Probe(handler=alive))
    out = json.loads(await handlers["playbook_publish"](explanation=EXPLANATION, name="greeter"))
    assert out["status"] == "published"
    gates = {g["gate"]: g for g in out["gates"]}
    assert gates["probes"]["note"] == "1 ok"


# ---- the playbook_preflight tool -------------------------------------------

@pytest.mark.asyncio
async def test_preflight_tool_reports_and_steers(env):
    sf, runner, handlers, tools = env
    await handlers["playbook_propose"](name="greeter", code=CODE)

    out = json.loads(await handlers["playbook_preflight"](name="greeter"))
    assert out["playbook"] == "greeter"
    assert out["checked_version"] == 1
    assert out["is_candidate"] is False
    assert (out["total"], out["unprobeable"], out["failed"]) == (1, 1, 0)
    assert "No tool declares a probe yet" in out["note"]

    # candidate exists → auto targets it; broken tool → BROKEN steering
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers, "greeter"),
        old="says hi", new="says hello",
    )

    async def dead():
        return {"ok": False, "failure_class": "resource_gone"}
    runner._tools._tools["send_chat_message"].definition = _Def(
        probe=_Probe(handler=dead)
    )
    out = json.loads(await handlers["playbook_preflight"](name="greeter"))
    assert out["checked_version"] == 2
    assert out["is_candidate"] is True
    assert out["failed"] == 1
    assert "BROKEN: send_chat_message (resource_gone)" in out["next"]

    # explicit live targeting still works
    out = json.loads(await handlers["playbook_preflight"](
        name="greeter", version="live",
    ))
    assert out["checked_version"] == 1
    assert out["is_candidate"] is False

    missing = json.loads(await handlers["playbook_preflight"](name="nope"))
    assert "not found" in missing["error"]


@pytest.mark.asyncio
async def test_preflight_tool_policy_and_gating(env):
    _, _, _, tools = env
    td = tools["playbook_preflight"][0]
    assert getattr(td, "policy", None) != "prompt_always"  # read-only check
    from plugin_playbooks import PlaybooksPlugin
    assert "playbook_preflight" in PlaybooksPlugin.AUTHORING_TOOLS
    skill_tools = {
        t for s in PlaybooksPlugin.manifest.skills for t in s.tools
    }
    assert "playbook_preflight" in skill_tools


# ---- daily sweep (reprobe_enabled) -----------------------------------------

@pytest.mark.asyncio
async def test_reprobe_alerts_only_on_new_failures(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)

    async def dead():
        return {"ok": False, "failure_class": "credential_dead"}
    reg = _Tools(send_chat_message=_Tool(_noop, _Def(probe=_Probe(handler=dead))))

    alerts = await reprobe_enabled(sf, reg)
    assert [(a["playbook"], a["tool"]) for a in alerts] == [
        ("greeter", "send_chat_message"),
    ]
    # same failure again → already known, stay silent
    assert await reprobe_enabled(sf, reg) == []

    # recovers → no alert; breaks again → alert fires again
    async def alive():
        return {"ok": True}
    reg_ok = _Tools(send_chat_message=_Tool(_noop, _Def(probe=_Probe(handler=alive))))
    assert await reprobe_enabled(sf, reg_ok) == []
    alerts = await reprobe_enabled(sf, reg)
    assert len(alerts) == 1


# ---- note wording ----------------------------------------------------------

def test_preflight_note_wording():
    assert preflight_note(
        {"total": 5, "ok": 2, "unprobeable": 3, "failed": 0}
    ) == "2 ok · 3 unprobeable"
    assert preflight_note(
        {"total": 1, "ok": 0, "unprobeable": 0, "failed": 1}
    ) == "1 failed"
    assert preflight_note(
        {"total": 0, "ok": 0, "unprobeable": 0, "failed": 0}
    ) == "no tools"
