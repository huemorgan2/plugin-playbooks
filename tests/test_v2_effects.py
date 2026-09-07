"""plans/032 phase 03 — ctx.llm, ctx.agent, ctx.subtask, ctx.gather, ctx.approve
(in-process form), `failed_handled`, the send_chat_message rule and `_timeout`
for the awaited kinds (docs/v2.md §2, §4, §6).

Every test drives the REAL shim through plugin-inline-code-run's managed
install (`tests/_jail.py`): the effects under test are about `run()` code
semantics — `try`/`except` around an effect, `gather` argument order, a
caught failure re-stamped `failed_handled` — which a scripted `code_run`
fake would only restate. Skipped without a usable kernel jail.

Fakes come from the v1 suite, as the repo plan requires: `_Agent`
(`tests/test_manifest_flow.py`, the `run_llm` fake), `FakeAgent` and the
event fakes (`tests/test_delegation.py`, the `run_turn` fake), `_Decision` /
`_Approvals` / `_Ctx` (`tests/test_repro_fixplaybooks_lifecycle.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import types
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from _jail import real_code_run, real_jail, requires_jail
from test_delegation import (
    FakeAgent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartStartEvent,
)
from test_manifest_flow import _Agent
from test_repro_fixplaybooks_lifecycle import _Approvals, _Ctx as _LifecycleCtx, _Decision
from plugin_playbooks.agent_tools import _nested_run_refusal
from plugin_playbooks.delegation import _TranscriptFeed
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner, active_run_id
from plugin_playbooks.v2 import APPROVE_RESULT_KEYS, MemoryJournalStore
from plugin_playbooks.v2.loop import SegmentLoop

pytestmark = [real_jail, requires_jail()]


# ------------------------------------------------------------------ harness
class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _Tool:
    def __init__(self, handler) -> None:
        self.handler = handler


class _Tools:
    def __init__(self, **tools) -> None:
        self._tools = tools

    def get(self, name: str):
        return self._tools[name]

    def add(self, name: str, handler) -> None:
        self._tools[name] = _Tool(handler)


class _Ctx(_LifecycleCtx):
    """The lifecycle `_Ctx` (`.approval`, `ops_conversation_id()`) plus what
    `_create_run` reads on any context: `current_conversation_id`."""

    current_conversation_id = None
    vault = None

    def __init__(self, approvals=None) -> None:
        super().__init__(approvals)


class _Dec(_Decision):
    def __init__(self, decision="approved", reason=None, decided_by="owner") -> None:
        super().__init__(decision, reason)
        self.decided_by = decided_by


class _GatedApprovals(_Approvals):
    """`request(**kw)` records the kw, blocks on `gate`, then answers with
    the configured decision; `get()` reports the configured status."""

    def __init__(self, decision: _Dec | None = None, status: str = "pending") -> None:
        super().__init__()
        self.decision = decision or _Dec()
        self.status = status
        self.gate = asyncio.Event()
        self.gets: list = []

    async def request(self, **kw):
        self.requests.append(kw)
        await self.gate.wait()
        return self.decision

    async def get(self, request_id):
        self.gets.append(request_id)
        return types.SimpleNamespace(status=self.status)


class _GatedApprovalsNoGet(_GatedApprovals):
    """The fallback path: an engine without `get`."""

    get = None  # type: ignore[assignment]


def _pb(name: str, source: str) -> Playbook:
    return Playbook(
        name=name, display_name=name, code=source,
        definition={"name": name, "steps": []}, status="enabled",
    )


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await asyncio.sleep(0.05)
    await engine.dispose()


@pytest.fixture
async def gated():
    """`fast` / `slow` (gated) tools as in the runtime repro file; the gate
    is released in teardown so no task outlives the test."""
    calls: list[str] = []
    gate = asyncio.Event()

    async def fast(**_kw):
        calls.append("fast")
        return {"ok": True, "which": "fast"}

    async def slow(**_kw):
        calls.append("slow-started")
        await gate.wait()
        calls.append("slow-finished")
        return {"ok": True, "which": "slow"}

    yield types.SimpleNamespace(calls=calls, gate=gate, fast=fast, slow=slow)
    gate.set()
    await asyncio.sleep(0.05)


def _tools(tmp_path, **tools) -> _Tools:
    t = _Tools(**{k: _Tool(v) for k, v in tools.items()})
    t.add("code_run", real_code_run(tmp_path))
    return t


def _runner(sf, tools, *, agent=None, context=None) -> tuple[PlaybookRunner, _Bus]:
    bus = _Bus()
    runner = PlaybookRunner(
        session_factory=sf, tool_registry=tools, events=bus, agent=agent, context=context,
    )
    runner._v2 = SegmentLoop(
        sf, tools, bus, context, MemoryJournalStore(keep_completed=True),
        agent=agent, start_run=runner.start_run,
    )
    return runner, bus


async def _save(sf, pb: Playbook) -> Playbook:
    async with sf() as s:
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
    return pb


async def _row(sf, run_id) -> PlaybookRun:
    async with sf() as s:
        return await s.get(PlaybookRun, run_id)


async def _runs(sf) -> list[PlaybookRun]:
    async with sf() as s:
        return list((await s.execute(select(PlaybookRun).order_by(PlaybookRun.started_at))).scalars().all())


async def _steps(sf, run_id) -> list[PlaybookStepRun]:
    async with sf() as s:
        return list((await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run_id)
            .order_by(PlaybookStepRun.started_at, PlaybookStepRun.id)
        )).scalars().all())


async def _run_to_end(runner, pb, inputs=None, timeout=60.0, **kw) -> PlaybookRun:
    run = await runner.start_run_background(pb, inputs=inputs or {}, **kw)
    row = await runner.wait_for_run(run.id, timeout=timeout)
    assert row is not None, "run did not finish in time"
    return row


def _journal(runner, run_id) -> list[dict]:
    return runner._v2.journal._runs[str(run_id)]


def _value(runner):
    return runner._v2.last_result.value


async def _until(pred, timeout=60.0, step=0.05) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(step)


# ------------------------------------------------------------------ 1-3 llm
async def test_llm_returns_dict_with_output_schema(db, tmp_path):
    agent = _Agent(result={"x": 1})
    runner, _ = _runner(db, _tools(tmp_path), agent=agent)
    pb = await _save(db, _pb("llm-dict", 'async def run(ctx, inputs):\n    return await ctx.llm("p", output={"type": "object"})\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", (row.error, row.traceback)
    assert _value(runner) == {"x": 1}
    prompt, kw = agent.calls[0]
    assert prompt == "p"
    assert kw["output_schema"] == {"type": "object"}
    assert kw["purpose"] == "summarization"


async def test_llm_returns_str_without_output(db, tmp_path):
    agent = _Agent(result="hi")
    runner, _ = _runner(db, _tools(tmp_path), agent=agent)
    pb = await _save(db, _pb("llm-str", 'async def run(ctx, inputs):\n    return await ctx.llm("p")\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", row.error
    assert _value(runner) == "hi"
    j = _journal(runner, row.id)
    assert j[1]["kind"] == "llm" and j[1]["status"] == "done" and j[1]["result"] == "hi"
    assert "cost_cents" not in j[1]  # `{"total_tokens": 1}` carries no cost


async def test_llm_records_cost_and_billing_scope(db, tmp_path, monkeypatch):
    import plugin_playbooks.runner as runner_mod

    rec = {"entered": 0, "active": False, "playbooks": [], "facade_saw": None}

    @contextlib.contextmanager
    def scope(playbook):
        rec["entered"] += 1
        rec["playbooks"].append(playbook.name)
        rec["active"] = True
        try:
            yield
        finally:
            rec["active"] = False

    monkeypatch.setattr(runner_mod, "_playbook_origin_scope", scope)

    class _CostAgent(_Agent):
        async def run_llm(self, prompt, **kw):
            rec["facade_saw"] = rec["active"]
            self.calls.append((prompt, kw))
            return self.result, types.SimpleNamespace(cost_cents=3)

    agent = _CostAgent(result="x")
    runner, _ = _runner(db, _tools(tmp_path), agent=agent)
    src = 'async def run(ctx, inputs):\n    return await ctx.llm("p")\n'
    pb = await _save(db, _pb("llm-cost", src))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", row.error
    assert _journal(runner, row.id)[1]["cost_cents"] == 3
    assert rec["entered"] == 1 and rec["playbooks"] == ["llm-cost"]
    assert rec["facade_saw"] is True

    # missing agent: the v1 error shape, the run fails
    runner2, _ = _runner(db, _tools(tmp_path), agent=None)
    pb2 = await _save(db, _pb("llm-noagent", src))
    row2 = await _run_to_end(runner2, pb2)
    assert row2.status == "failed"
    assert "requires an injected agent" in row2.error
    assert _journal(runner2, row2.id)[1]["status"] == "failed"


# ------------------------------------------------------------------ 4-5 agent
async def test_agent_transcript_on_entry_and_nested_guard(db, tmp_path):
    seen: dict = {}

    class _GuardedAgent(FakeAgent):
        async def run_turn(self, prompt, **kwargs):
            seen["active_run_id"] = active_run_id()
            seen["refusal"] = json.loads(_nested_run_refusal())
            return await super().run_turn(prompt, **kwargs)

    script = [
        FunctionToolCallEvent("t", "c1"),
        FunctionToolResultEvent("t", "c1", "done"),
        PartStartEvent("thinking"),
    ]
    agent = _GuardedAgent(result="fine", events=script)
    runner, _ = _runner(db, _tools(tmp_path), agent=agent)
    pb = await _save(db, _pb("agent-tx", 'async def run(ctx, inputs):\n    return await ctx.agent("do it", tools=["t"])\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", (row.error, row.traceback)
    assert _value(runner) == "fine"
    assert seen["active_run_id"] == str(row.id)
    assert seen["refusal"]["gate"] == "nested_playbook_run"
    call = agent.calls[0]
    assert call["tools"] == ["t"]
    assert call["memory_write"] is False
    assert call["conversation_id"] == row.report_to
    assert call["output_schema"] is None
    entry = _journal(runner, row.id)[1]
    assert entry["kind"] == "agent" and entry["status"] == "done"
    # the transcript is what the same script yields through the delegation feed
    ref = _TranscriptFeed()

    async def _stream():
        for ev in script:
            yield ev

    await ref.handle(None, _stream())
    strip = lambda evs: [{k: v for k, v in e.items() if k not in ("ts", "ms")} for e in evs]  # noqa: E731
    assert strip(entry["transcript"]) == strip(ref.events)
    assert [e["kind"] for e in entry["transcript"]] == ["tool", "thought"]
    assert entry["transcript"][0]["label"] == "t" and entry["transcript"][0]["ok"] is True
    assert entry["transcript"][0]["detail"] == "done"
    assert entry["transcript"][1]["label"] == "thinking"


async def test_agent_aborted_answer_fails_loud(db, tmp_path):
    agent = FakeAgent(result={"_aborted": "timeout", "error": "turn limit"})
    runner, _ = _runner(db, _tools(tmp_path), agent=agent)
    pb = await _save(db, _pb("agent-abort", 'async def run(ctx, inputs):\n    return await ctx.agent("do it", output={"type": "object"})\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "failed"
    assert "turn limit" in row.error
    entry = _journal(runner, row.id)[1]
    assert entry["status"] == "failed" and entry["error"]["type"] == "EffectError"
    assert "turn limit" in entry["error"]["message"]


# ------------------------------------------------------------------ 6-7 gather
async def test_gather_orders_results_and_runs_concurrently(db, tmp_path, gated):
    runner, _ = _runner(db, _tools(tmp_path, fast=gated.fast, slow=gated.slow))
    pb = await _save(db, _pb("gather", 'async def run(ctx, inputs):\n    a, b = await ctx.gather(ctx.tool("slow"), ctx.tool("fast"))\n    return [a, b]\n'))
    run = await runner.start_run_background(pb, inputs={})
    await _until(lambda: "fast" in gated.calls and "slow-started" in gated.calls)
    assert "slow-finished" not in gated.calls
    gated.gate.set()
    row = await runner.wait_for_run(run.id, timeout=60)
    assert row.status == "done", (row.error, row.traceback)
    assert _value(runner) == [{"ok": True, "which": "slow"}, {"ok": True, "which": "fast"}]
    j = _journal(runner, row.id)
    assert [(e["seq"], e["name"], e["status"]) for e in j[1:]] == [(1, "slow", "done"), (2, "fast", "done")]
    # one gather exit carried both pending effects, seq 1 and 2 in argument order
    code_run = runner._v2._tools.get("code_run").handler
    gather_exits = [p["result"] for p in code_run.payloads if p.get("result", {}).get("kind") == "gather"]
    assert len(gather_exits) == 1
    assert [(e["seq"], e["name"]) for e in gather_exits[0]["effects"]] == [(1, "slow"), (2, "fast")]
    assert gather_exits[0]["seq"] == 1
    assert code_run.payloads[-1]["result"]["kind"] == "return"


async def test_gather_raises_first_failure_after_all_settle(db, tmp_path, gated):
    async def bad1(**_kw):
        gated.calls.append("bad1")
        raise RuntimeError("bad1 broke")

    async def bad2(**_kw):
        gated.calls.append("bad2")
        raise RuntimeError("bad2 broke")

    runner, _ = _runner(db, _tools(tmp_path, bad1=bad1, bad2=bad2, slow=gated.slow))
    src = (
        'async def run(ctx, inputs):\n'
        '    try:\n'
        '        res = await ctx.gather(ctx.tool("bad1"), ctx.tool("slow"), ctx.tool("bad2"))\n'
        '    except ctx.ToolError as e:\n'
        '        return str(e)\n'
        '    return res\n'
    )
    pb = await _save(db, _pb("gather-fail", src))
    run = await runner.start_run_background(pb, inputs={})
    await _until(lambda: "bad1" in gated.calls and "bad2" in gated.calls and "slow-started" in gated.calls)
    await asyncio.sleep(0.2)
    assert (await _row(db, run.id)).status == "running"  # waits for slow to settle
    gated.gate.set()
    row = await runner.wait_for_run(run.id, timeout=60)
    assert row.status == "done", (row.error, row.traceback)
    assert "bad1" in _value(runner) and "bad2" not in _value(runner)
    assert "slow-finished" in gated.calls
    j = _journal(runner, row.id)
    assert [(e["name"], e["status"]) for e in j[1:]] == [
        ("bad1", "failed_handled"), ("slow", "done"), ("bad2", "failed_handled"),
    ]
    assert j[1]["error"]["type"] == "ToolError" and j[3]["error"]["type"] == "ToolError"


# ------------------------------------------------------------------ 8-11 subtask
CHILD_DOUBLE = 'async def run(ctx, inputs):\n    return {"n": inputs["n"] * 2}\n'


async def test_subtask_returns_child_value_and_links_rows(db, tmp_path):
    runner, bus = _runner(db, _tools(tmp_path))
    await _save(db, _pb("child", CHILD_DOUBLE))
    parent_pb = await _save(db, _pb("parent", 'async def run(ctx, inputs):\n    return await ctx.subtask("child", {"n": 2})\n'))
    parent = await _run_to_end(runner, parent_pb)
    assert parent.status == "done", (parent.error, parent.traceback)
    assert _value(runner) == {"n": 4}
    rows = await _runs(db)
    assert len(rows) == 2
    child = next(r for r in rows if r.id != parent.id)
    assert child.parent_run_id == parent.id
    assert child.trigger == f"subtask:{parent.id}"
    assert child.is_test == parent.is_test
    assert child.status == "done" and parent.status == "done"
    assert child.inputs == {"n": 2}
    entry = _journal(runner, parent.id)[1]
    assert entry["kind"] == "subtask" and entry["name"] == "child" and entry["status"] == "done"
    assert entry["child_run_id"] == str(child.id)
    assert entry["result"] == {"n": 4}
    assert runner._v2._values == {}  # the child's value was consumed


async def test_subtask_cycle_guard_trips_with_existing_refusal(db, tmp_path):
    runner, _ = _runner(db, _tools(tmp_path))
    a = await _save(db, _pb("cyc-a", 'async def run(ctx, inputs):\n    return await ctx.subtask("cyc-b", {})\n'))
    await _save(db, _pb("cyc-b", 'async def run(ctx, inputs):\n    return await ctx.subtask("cyc-a", {})\n'))
    row_a = await _run_to_end(runner, a)
    rows = await _runs(db)
    assert len(rows) == 2  # a, b — no third row for the refused a
    row_b = next(r for r in rows if r.id != row_a.id)
    assert row_b.parent_run_id == row_a.id
    assert row_b.status == "failed" and row_a.status == "failed"
    assert "would recurse" in row_b.error and "cyc-a" in row_b.error and "cyc-b" in row_b.error
    assert row_b.error_type == "EffectError"
    assert row_a.error_type == "SubtaskFailed"
    jb = _journal(runner, row_b.id)
    assert jb[1]["status"] == "failed" and jb[1]["error"]["type"] == "EffectError"
    assert "would recurse" in jb[1]["error"]["message"]


async def test_subtask_child_failure_is_catchable(db, tmp_path):
    runner, _ = _runner(db, _tools(tmp_path))
    await _save(db, _pb("child-boom", 'async def run(ctx, inputs):\n    raise ValueError("boom")\n'))
    parent_pb = await _save(db, _pb("parent-catch", (
        'async def run(ctx, inputs):\n'
        '    try:\n'
        '        return await ctx.subtask("child-boom", {})\n'
        '    except ctx.EffectError as e:\n'
        '        return str(e)\n'
    )))
    parent = await _run_to_end(runner, parent_pb)
    assert parent.status == "done", (parent.error, parent.traceback)
    rows = await _runs(db)
    child = next(r for r in rows if r.id != parent.id)
    assert child.status == "failed" and "boom" in child.error
    value = _value(runner)
    assert "boom" in value and str(child.id) in value
    entry = _journal(runner, parent.id)[1]
    assert entry["status"] == "failed_handled"
    assert entry["error"]["type"] == "SubtaskFailed"
    assert entry["child_run_id"] == str(child.id)


async def test_subtask_unknown_playbook_uses_v1_message(db, tmp_path):
    runner, _ = _runner(db, _tools(tmp_path))
    pb = await _save(db, _pb("parent-nope", 'async def run(ctx, inputs):\n    return await ctx.subtask("nope", {})\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "failed"
    assert "Subtask playbook 'nope' not found" in row.error
    entry = _journal(runner, row.id)[1]
    assert entry["status"] == "failed"
    assert entry["error"]["message"] == "Subtask playbook 'nope' not found"
    assert len(await _runs(db)) == 1


# ------------------------------------------------------------------ 12-14 approve
APPROVE_SRC = (
    'async def run(ctx, inputs):\n'
    '    r = await ctx.approve(show={"summary": "x"})\n'
    '    await ctx.tool("fast")\n'
    '    return r\n'
)


async def test_approve_blocks_until_decided(db, tmp_path, gated):
    """v2 twin of test_repro_fixplaybooks_runtime::test_wait_for_approval_actually_gates."""
    approvals = _GatedApprovals(_Dec("approved", reason="looks good", decided_by="owner"))
    runner, _ = _runner(db, _tools(tmp_path, fast=gated.fast), context=_Ctx(approvals))
    pb = await _save(db, _pb("approve", APPROVE_SRC))
    run = await runner.start_run_background(pb, inputs={})
    await _until(lambda: len(approvals.requests) == 1)
    await asyncio.sleep(0.2)
    assert "fast" not in gated.calls
    assert (await _row(db, run.id)).status == "running"
    kw = approvals.requests[0]
    assert kw["kind"] == "playbook_effect"
    assert kw["requested_by_plugin"] == "plugin-playbooks"
    assert kw["risk_level"] == "medium"
    assert kw["payload"] == {"run_id": str(run.id), "seq": 1, "playbook": "approve", "version": run.playbook_version}
    assert kw["presentation"]["changes"] and kw["presentation"]["changes"][0]["kind"] == "text"
    assert '"summary": "x"' in kw["presentation"]["changes"][0]["text"]
    assert kw["presentation"]["headline"]
    assert kw["conversation_id"] == run.report_to
    assert kw["ttl_seconds"] is None
    approvals.gate.set()
    row = await runner.wait_for_run(run.id, timeout=60)
    assert row.status == "done", (row.error, row.traceback)
    assert "fast" in gated.calls
    entry = _journal(runner, row.id)[1]
    assert entry["kind"] == "approve" and entry["status"] == "done"
    assert entry["result"]["approved"] is True
    assert entry["result"]["request_id"] == str(approvals.decision.request_id)
    assert entry["result"]["reason"] == "looks good" and entry["result"]["decided_by"] == "owner"
    assert set(entry["result"]) == APPROVE_RESULT_KEYS
    assert _value(runner) == entry["result"]


@pytest.mark.parametrize("caught", [True, False])
async def test_approve_rejected_raises_ctx_rejected_catchable(db, tmp_path, gated, caught):
    approvals = _GatedApprovals(_Dec("rejected", reason="no"))
    approvals.gate.set()
    runner, _ = _runner(db, _tools(tmp_path, fast=gated.fast), context=_Ctx(approvals))
    if caught:
        src = (
            'async def run(ctx, inputs):\n'
            '    try:\n'
            '        await ctx.approve(show="x")\n'
            '    except ctx.Rejected as e:\n'
            '        return f"rejected: {e}"\n'
            '    return "approved"\n'
        )
    else:
        src = APPROVE_SRC
    pb = await _save(db, _pb("approve-rej", src))
    row = await _run_to_end(runner, pb)
    entry = _journal(runner, row.id)[1]
    assert entry["error"]["type"] == "Rejected"
    if caught:
        assert row.status == "done", (row.error, row.traceback)
        assert _value(runner).startswith("rejected: ") and "no" in _value(runner)
        assert entry["status"] == "failed_handled"
    else:
        assert row.status == "failed"
        assert row.error_type == "Rejected"
        assert entry["status"] == "failed"
        assert "fast" not in gated.calls


@pytest.mark.parametrize("has_get", [True, False])
async def test_approve_expired_raises_ctx_approval_expired(db, tmp_path, has_get):
    decision = _Dec("rejected", reason="ttl elapsed", decided_by="system")
    approvals = _GatedApprovals(decision, status="expired") if has_get else _GatedApprovalsNoGet(decision)
    approvals.gate.set()
    runner, _ = _runner(db, _tools(tmp_path), context=_Ctx(approvals))
    src = (
        'async def run(ctx, inputs):\n'
        '    try:\n'
        '        await ctx.approve(show="x", _timeout=5)\n'
        '    except ctx.ApprovalExpired as e:\n'
        '        return f"expired: {e}"\n'
        '    return "approved"\n'
    )
    pb = await _save(db, _pb("approve-exp", src))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", (row.error, row.traceback)
    assert _value(runner).startswith("expired: ")
    entry = _journal(runner, row.id)[1]
    assert entry["status"] == "failed_handled" and entry["error"]["type"] == "ApprovalExpired"
    assert approvals.requests[0]["ttl_seconds"] == 5
    if has_get:
        assert approvals.gets == [decision.request_id]


# ------------------------------------------------------------------ 15 failed_handled
async def test_handled_tool_failure_is_failed_handled_and_run_completes(db, tmp_path, gated):
    async def bad(**_kw):
        raise RuntimeError("nope")

    runner, _ = _runner(db, _tools(tmp_path, bad=bad, fast=gated.fast))
    src = (
        'async def run(ctx, inputs):\n'
        '    try:\n'
        '        await ctx.tool("bad")\n'
        '    except ctx.ToolError:\n'
        '        pass\n'
        '    await ctx.tool("fast")\n'
        '    return "ok"\n'
    )
    pb = await _save(db, _pb("handled", src))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", (row.error, row.traceback)
    j = _journal(runner, row.id)
    assert j[1]["status"] == "failed_handled" and j[1]["error"]["type"] == "ToolError"
    assert j[2]["status"] == "done"
    assert _value(runner) == "ok"


# ------------------------------------------------------------------ 16 send_chat_message
async def test_send_chat_message_conversation_rules_match_v1(db, tmp_path):
    received: list[dict] = []

    async def send_chat_message(**kw):
        received.append(kw)
        return {"sent": True}

    src = 'async def run(ctx, inputs):\n    await ctx.tool("send_chat_message", text="hi")\n    return "sent"\n'
    # a chat-invoked run inherits the run's stamped report_to
    ctx = _Ctx(None)
    ctx.current_conversation_id = uuid.uuid4()
    runner, _ = _runner(db, _tools(tmp_path, send_chat_message=send_chat_message), context=ctx)
    pb = await _save(db, _pb("chat", src))
    row = await _run_to_end(runner, pb, trigger="agent")
    assert row.status == "done", (row.error, row.traceback)
    assert row.report_to == ctx.current_conversation_id
    assert received == [{"text": "hi", "conversation_id": str(row.report_to)}]
    j = _journal(runner, row.id)
    # the journal keeps the code's own args (replay compares them); the
    # injected conversation is what the tool received
    assert j[1]["args"] == {"text": "hi"} and j[1]["status"] == "done"
    steps = await _steps(db, row.id)
    assert steps[0].status == "done"

    # a background live run: no report chat → the effect fails, the run fails
    ctx2 = _Ctx(None)
    runner2, _ = _runner(db, _tools(tmp_path, send_chat_message=send_chat_message), context=ctx2)
    pb2 = await _save(db, _pb("chat-bg", src))
    row2 = await _run_to_end(runner2, pb2)
    assert row2.report_to is None and row2.is_test is False
    assert row2.status == "failed"
    assert "this run has no chat to report to" in row2.error
    j2 = _journal(runner2, row2.id)
    assert j2[1]["status"] == "failed" and j2[1]["error"]["type"] == "ToolError"
    assert "this run has no chat to report to" in j2[1]["error"]["message"]
    assert len(received) == 1


# ------------------------------------------------------------------ 17 timeouts
class _StuckAgent(_Agent):
    """`run_llm` parks on an event that the test releases in teardown."""

    def __init__(self) -> None:
        super().__init__(result="never")
        self.gate = asyncio.Event()

    async def run_llm(self, prompt, **kw):
        self.calls.append((prompt, kw))
        await self.gate.wait()
        return self.result, {"total_tokens": 1}


_TIMEOUT_CALL = {
    "llm": 'ctx.llm("p", _timeout=1)',
    "agent": 'ctx.agent("p", _timeout=1)',
    # the child needs one jail segment (~0.4 s idle, several under load) to
    # reach its blocked tool before the deadline lands
    "subtask": 'ctx.subtask("slow-child", {}, _timeout=3)',
}


@pytest.mark.parametrize("caught", [False, True])
@pytest.mark.parametrize("kind", ["llm", "agent", "subtask"])
async def test_effect_timeout_enforced_for_llm_agent_subtask(db, tmp_path, gated, kind, caught):
    if kind == "llm":
        agent = _StuckAgent()
    elif kind == "agent":
        agent = FakeAgent(gate=asyncio.Event())
    else:
        agent = None
    runner, _ = _runner(db, _tools(tmp_path, slow=gated.slow), agent=agent)
    if kind == "subtask":
        await _save(db, _pb("slow-child", 'async def run(ctx, inputs):\n    await ctx.tool("slow")\n    return "child"\n'))
    if caught:
        src = (
            'async def run(ctx, inputs):\n'
            '    try:\n'
            f'        await {_TIMEOUT_CALL[kind]}\n'
            '    except ctx.EffectTimeout:\n'
            '        return "late"\n'
            '    return "on time"\n'
        )
    else:
        src = f'async def run(ctx, inputs):\n    await {_TIMEOUT_CALL[kind]}\n    return "on time"\n'
    pb = await _save(db, _pb(f"timeout-{kind}", src))
    try:
        run = await runner.start_run_background(pb, inputs={})
        # the deadline under test is the 1 s `_timeout` (asserted through the
        # journal row); the wait here only bounds a loaded machine's segments
        row = await runner.wait_for_run(run.id, timeout=90)
        assert row is not None and row.status != "running", "the effect did not time out"
        entry = _journal(runner, run.id)[1]
        assert entry["kind"] == kind
        assert entry["error"]["type"] == "EffectTimeout"
        assert "timed out" in entry["error"]["message"]
        if caught:
            assert row.status == "done", (row.error, row.traceback)
            assert _value(runner) == "late"
            assert entry["status"] == "failed_handled"
        else:
            assert row.status == "failed"
            assert row.error_type == "EffectTimeout"
            assert "timed out" in row.error
            assert entry["status"] == "failed"
        if kind == "subtask":
            rows = await _runs(db)
            child = next(r for r in rows if r.id != run.id)
            assert child.status == "cancelled"
            assert entry["child_run_id"] == str(child.id)
            assert "slow-finished" not in gated.calls
            # the deadline lands wherever the child's first jail segment got
            # to (under load, before it journals `slow`): what holds either
            # way is that no row is left `in_flight` and any effect row it did
            # journal fails RunCancelled (loop.py `_journal_and_start` /
            # `_execute_and_finish`). The stamp itself is pinned race-free by
            # test_v2_loop.py::test_cancel_run_v2_uncatchable.
            cj = _journal(runner, child.id)
            assert cj[0]["kind"] == "run"
            assert all(r.get("status") != "in_flight" for r in cj)
            for r in cj[1:]:
                assert r["kind"] == "tool" and r["name"] == "slow", r
                assert r["status"] == "failed" and r["error"]["type"] == "RunCancelled", r
        else:
            assert len(agent.calls) == 1
    finally:
        if agent is not None:
            agent.gate.set()
