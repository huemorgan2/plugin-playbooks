"""plans/032 phase 08 (part 2a) — the per-run owner card: `playbook_run` on
an `agent_must_confirm` playbook raises a `playbook_run` card and parks the
run on it (both formats) instead of telling the agent to grant itself
permanent autonomy; `manual_only` is refused without a card; the
`playbook_set_autonomy` result says the change is permanent; cards raised by
a candidate's test run are labelled (master §2 Lifecycle, phase 08 Steps 7-9).

Harness: `_ParkBus` + `_NowaitApprovals` of tests/test_v2_parked.py (a
pending card, `decide` emits `approval.decided`), real `PlaybookRunner`,
`build_tools`; the python twin runs the scripted `code_run` (no jail), the
pblang twin the v1 step machinery on the same fake tool.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from test_repro_fixplaybooks_runtime import _Tool, _Tools
from test_v2_loop import ScriptedCodeRun, _pb
from test_v2_parked import (
    _Calls, _Ctx, _Dec, _Env, _NowaitApprovals, _ParkBus, _journal, _prog, _row, _settle,
    _until, db,  # noqa: F401 — fixture
)
from test_v2_parked import APPROVE, FAST
from test_v2_resume import _save

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Playbook, PlaybookRun, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.wake import RunCompletionWake

PY_SRC = (
    "async def run(ctx, inputs):\n"
    "    await ctx.tool('fast')\n"
    "    return {'n': 1}\n"
)


def _v1(name: str) -> Playbook:
    """The pblang greeter of the runtime repro, on the shared `fast` tool."""
    return Playbook(
        name=name, display_name=name, status="enabled",
        definition={"name": name, "steps": [
            {"id": "say", "kind": "tool_call", "tool": "fast", "inputs": {}},
        ]},
    )


class _WakeCtx(_Ctx):
    """A wake-capable core pinned to one conversation."""

    def __init__(self, approvals, origin) -> None:
        super().__init__(approvals)
        self.current_conversation_id = origin
        self.sent: list[dict] = []

    async def send_muted_message(self, title, content, **kw):
        self.sent.append({"title": title, "content": content, **kw})
        return {"responded": True}


def _env(sf, script=None, *, ctx=None) -> _Env:
    calls = _Calls()
    tools = {"fast": _Tool(calls.fast)}
    tools["code_run"] = _Tool(ScriptedCodeRun(script or _prog([FAST], value={"n": 1})).handler)
    registry = _Tools(**tools)
    bus = _ParkBus()
    approvals = _NowaitApprovals(bus)
    ctx = ctx or _Ctx(approvals)
    ctx.approval = approvals
    runner = PlaybookRunner(session_factory=sf, tool_registry=registry, events=bus, context=ctx)
    runner.park.start()
    env = _Env(sf, registry, calls, bus, approvals, ctx, runner)
    env.tools_by_name = {td.name: h for td, h in build_tools(sf, bus, runner, ctx)}  # type: ignore[attr-defined]
    env.defs = {td.name: td for td, _ in build_tools(sf, bus, runner, ctx)}  # type: ignore[attr-defined]
    return env


async def _confirm_pb(sf, pb: Playbook) -> Playbook:
    pb.agent_autonomy = "agent_must_confirm"
    return await _save(sf, pb)


async def _run_tool(env: _Env, name: str) -> dict:
    return json.loads(await env.tools_by_name["playbook_run"](name=name, inputs="{}", wait_seconds=10))


def _assert_card_text(out: dict, run_id: str, aid: str) -> None:
    assert out["status"] == "parked" and out["run_id"] == run_id and out["approval_id"] == aid, out
    assert out["message"] == f"run {run_id} waiting on owner card #{aid} — tell the user; nothing to poll"
    text = json.dumps(out)
    assert "playbook_set_autonomy" not in text and "needs_approval" not in text


def _assert_card(env: _Env, name: str, version: int) -> str:
    assert env.approvals.request_calls == []  # the park form, never `request`
    assert len(env.approvals.nowait_calls) == 1
    kw = env.approvals.nowait_calls[0]
    assert kw["kind"] == "playbook_run"
    assert kw["payload"] == {"playbook": name, "version": version, "inputs": {}}
    assert kw["summary"] == f"Run playbook '{name}' v{version} (agent request)"
    assert kw["requested_by_plugin"] == "plugin-playbooks" and kw["risk_level"] == "medium"
    assert kw["presentation"]["eyebrow"] == "Playbook run"
    assert kw["presentation"]["headline"] == name and kw["presentation"]["changes"] == []
    assert kw["presentation"]["explanation"].startswith("The agent asked to run this playbook now.")
    return next(iter(env.approvals.cards))


async def _steps(sf, run_id) -> list[PlaybookStepRun]:
    async with sf() as s:
        return list((await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run_id)
        )).scalars().all())


# ------------------------------------------------------------------ 1 v2 parks at effect 0
async def test_agent_must_confirm_v2_parks_at_effect_0(db):
    env = _env(db)
    await _confirm_pb(db, _pb("py", PY_SRC))
    out = await _run_tool(env, "py")
    run_id = uuid.UUID(out["run_id"])
    aid = _assert_card(env, "py", 1)
    _assert_card_text(out, str(run_id), aid)
    row = await _row(db, run_id)
    assert row.status == "parked" and row.parked_on["kind"] == "approval"
    assert row.parked_on["approval_id"] == aid and row.parked_on["gate"] == "run"
    assert row.format == "python"
    # no journal at all — the park precedes entry 0 (nothing ran)
    try:
        entries = await _journal(db, run_id)
    except KeyError:
        entries = []
    assert entries == []
    assert env.calls.calls == []
    parked = env.bus.named("playbook.run.parked")
    assert len(parked) == 1 and parked[0]["run_id"] == str(run_id) and parked[0]["seq"] == 0
    assert parked[0]["parked_on"]["approval_id"] == aid
    assert env.runner._tasks == {}  # no task until the owner decides
    # approve → the first segment runs, the run completes done
    await env.approvals.decide(aid, _Dec("approved", decided_by="owner"))
    done = await env.wait_completed()
    assert done["run_id"] == str(run_id) and done["status"] == "done" and done["result"] == {"n": 1}
    assert env.calls.calls == ["fast"]
    row = await _row(db, run_id)
    assert row.status == "done" and row.parked_on is None

    # a second run, rejected → failed / Rejected, nothing ran
    env.approvals.nowait_calls.clear()
    env.approvals.cards.clear()
    env.bus.events.clear()
    out2 = await _run_tool(env, "py")
    aid2 = next(iter(env.approvals.cards))
    _assert_card_text(out2, out2["run_id"], aid2)
    await env.approvals.decide(aid2, _Dec("rejected", reason="not now", decided_by="owner"))
    failed = await env.wait_completed()
    assert failed["run_id"] == out2["run_id"] and failed["status"] == "failed"
    row2 = await _row(db, uuid.UUID(out2["run_id"]))
    assert row2.status == "failed" and row2.error_type == "Rejected"
    assert row2.error == f"run rejected by owner card #{aid2}: not now"
    assert env.calls.calls == ["fast"]  # only the first run's tool call
    st = json.loads(await env.tools_by_name["playbook_status"](run_id=out2["run_id"]))
    assert st["status"] == "failed" and st["error_type"] == "Rejected"


# ------------------------------------------------------------------ 2 v1 awaits in the task
async def test_agent_must_confirm_v1_awaits_in_the_task(db):
    env = _env(db)
    await _confirm_pb(db, _v1("greeter"))
    out = await _run_tool(env, "greeter")
    run_id = uuid.UUID(out["run_id"])
    aid = _assert_card(env, "greeter", 1)
    _assert_card_text(out, str(run_id), aid)
    row = await _row(db, run_id)
    assert row.status == "parked" and row.format == "pblang"
    assert row.parked_on["approval_id"] == aid and row.parked_on["gate"] == "run"
    # the same playbook_status text as a v2 park
    st = json.loads(await env.tools_by_name["playbook_status"](run_id=str(run_id)))
    assert st["status"] == "parked"
    assert st["hint"].startswith(f"parked on approval #{aid} — nothing to poll")
    # nothing ran before the decision: no activity, no heartbeat, no step row
    names = [n for n, _ in env.bus.events]
    assert "activity.started" not in names and "activity.heartbeat" not in names
    assert await _steps(db, run_id) == []
    assert env.calls.calls == []
    assert run_id in env.runner._tasks  # the v1 task is alive, waiting
    await env.approvals.decide(aid, _Dec("approved", decided_by="owner"))
    done = await env.wait_completed()
    assert done["run_id"] == str(run_id) and done["status"] == "done" and done["result"] is None
    assert env.calls.calls == ["fast"]
    names = [n for n, _ in env.bus.events]
    assert names.index("approval.decided") < names.index("activity.started")
    steps = await _steps(db, run_id)
    assert [s.step_id for s in steps] == ["say"] and steps[0].status == "done"
    row = await _row(db, run_id)
    assert row.status == "done" and row.parked_on is None
    await _settle()
    assert env.runner._tasks == {}

    # rejection: the waiting task ends without running anything
    env.approvals.nowait_calls.clear()
    env.approvals.cards.clear()
    env.bus.events.clear()
    out2 = await _run_tool(env, "greeter")
    aid2 = next(iter(env.approvals.cards))
    await env.approvals.decide(aid2, _Dec("rejected", decided_by="owner"))
    failed = await env.wait_completed()
    assert failed["status"] == "failed"
    row2 = await _row(db, uuid.UUID(out2["run_id"]))
    assert row2.status == "failed" and row2.error_type == "Rejected"
    assert row2.error == f"run rejected by owner card #{aid2}"
    assert env.calls.calls == ["fast"]
    assert "activity.started" not in [n for n, _ in env.bus.events]
    await _settle()
    assert env.runner._tasks == {}


# ------------------------------------------------------------------ 3 wake promised
async def test_wake_promised_for_parked_run(db):
    origin = uuid.uuid4()
    ctx = _WakeCtx(None, origin)  # `_env` installs the approval engine
    env = _env(db, ctx=ctx)
    wake = RunCompletionWake(db, env.bus, ctx)
    wake.start()
    for name, pb in (("py", _pb("py", PY_SRC)), ("greeter", _v1("greeter"))):
        env.approvals.cards.clear()
        env.approvals.nowait_calls.clear()
        env.bus.events.clear()
        await _confirm_pb(db, pb)
        out = await _run_tool(env, name)
        run_id = uuid.UUID(out["run_id"])
        row = await _row(db, run_id)
        assert row.status == "parked" and row.wake_on_complete is True, name
        assert str(row.conversation_id) == str(origin)
        kw = env.approvals.nowait_calls[0]
        assert str(kw["conversation_id"]) == str(origin)  # the card wakes the origin chat
        aid = next(iter(env.approvals.cards))
        await env.approvals.decide(aid, _Dec("approved", decided_by="owner"))
        await env.wait_completed()
        await _until(lambda: not wake._tasks)
        # completion after the approval woke the origin conversation
        assert ctx.sent and str(ctx.sent[-1]["conversation_id"]) == str(origin), name
        assert out["run_id"] in ctx.sent[-1]["content"]
        ctx.sent.clear()
    wake.stop()


# ------------------------------------------------------------------ 4 manual_only
async def test_manual_only_refuses_without_autonomy_advice(db):
    env = _env(db)
    pb = _pb("py", PY_SRC)
    pb.agent_autonomy = "manual_only"
    await _save(db, pb)
    out = await _run_tool(env, "py")
    assert out == {
        "status": "refused",
        "playbook": "py",
        "reason": (
            "This playbook is manual_only — the owner runs it from the playbook "
            "page. Do not change its autonomy on your own."
        ),
    }
    assert "playbook_set_autonomy" not in json.dumps(out) and "needs_approval" not in json.dumps(out)
    assert env.approvals.nowait_calls == [] and env.approvals.request_calls == []
    async with db() as s:
        assert (await s.execute(select(PlaybookRun))).scalars().all() == []
    # the old self-grant advice is gone from the tool contract too
    assert "playbook_set_autonomy" not in env.defs["playbook_run"].description
    assert "per-run owner card" in env.defs["playbook_run"].description


# ------------------------------------------------------------------ 5 set_autonomy
async def test_set_autonomy_states_permanence(db):
    env = _env(db)
    await _save(db, _pb("py", PY_SRC))
    out = json.loads(await env.tools_by_name["playbook_set_autonomy"](
        name="py", why="the owner asked", agent_autonomy="agent_may_trigger",
    ))
    assert out["status"] == "updated" and out["new_autonomy"] == "agent_may_trigger"
    assert "PERMANENT" in out["note"] and "per-run card" in out["note"]
    assert "every future run of 'py'" in out["note"]
    desc = env.defs["playbook_set_autonomy"].description
    assert "per-run owner card" in desc and "PERMANENTLY" in desc
    # both notes survive together
    both = json.loads(await env.tools_by_name["playbook_set_autonomy"](
        name="py", why="x", agent_autonomy="agent_must_confirm", publish_autonomy="auto",
    ))
    assert "PERMANENT" in both["note"] and "no longer changes publishing" in both["note"]
    # require_run alone carries no permanence note (nothing about runs changed)
    only = json.loads(await env.tools_by_name["playbook_set_autonomy"](name="py", why="x", require_run=False))
    assert "note" not in only


# ------------------------------------------------------------------ 6 test-run label
async def test_candidate_test_run_cards_are_labelled(db):
    env = _env(db, _prog([APPROVE, FAST]))
    pb = _pb("py", "async def run(ctx, inputs):\n    await ctx.approve(show={'summary': 'x'})\n")
    pb.version, pb.live_version, pb.candidate_version = 3, 1, 3
    await _save(db, pb, versions={1: PY_SRC, 3: pb.code})
    out = json.loads(await env.tools_by_name["playbook_run_candidate"](name="py", inputs="{}", wait_seconds=10))
    assert out["status"] == "parked", out
    run_id = uuid.UUID(out["run_id"])
    row = await _row(db, run_id)
    assert row.is_test is True and row.playbook_version == 3
    kw = env.approvals.nowait_calls[0]
    assert kw["kind"] == "playbook_effect"
    assert kw["presentation"]["eyebrow"] == "test run of candidate v3"
    assert kw["summary"].startswith("[test run of candidate v3] ")
    # plugin/03's payload shape, untouched (identity)
    assert kw["payload"] == {"run_id": str(run_id), "seq": 1, "playbook": "py", "version": 3}
    # a per-run card raised for an is_test run carries the same label
    class _Run:
        is_test = True
        playbook_version = 3
        id = run_id
        playbook_id = row.playbook_id
        conversation_id = None
        trigger = "agent-candidate"
        parent_run_id = None
        format = "python"

    card = await env.runner._run_card_kw(_Run(), pb, {"a": 1})
    assert card["kind"] == "playbook_run"
    assert card["presentation"]["eyebrow"] == "test run of candidate v3"
    assert card["summary"] == "[test run of candidate v3] Run playbook 'py' v3 (agent request)"
    assert card["payload"] == {"playbook": "py", "version": 3, "inputs": {"a": 1}}
    # a live (non-test) run keeps plugin/03's eyebrow
    aid = next(iter(env.approvals.cards))
    await env.approvals.decide(aid, _Dec("approved", decided_by="owner"))
    await env.wait_completed()
    live_env = _env(db, _prog([APPROVE, FAST]))
    pb2 = _pb("live", pb.code)
    pb2.agent_autonomy = "agent_may_trigger"
    await _save(db, pb2)
    out2 = json.loads(await live_env.tools_by_name["playbook_run"](name="live", inputs="{}", wait_seconds=10))
    assert out2["status"] == "parked"
    kw2 = live_env.approvals.nowait_calls[0]
    assert kw2["presentation"]["eyebrow"] == "Playbook approval"
    assert kw2["summary"].startswith("Playbook 'live' is asking for your approval")
