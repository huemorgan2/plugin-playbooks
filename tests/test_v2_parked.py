"""plans/032 phase 07 — parked runs: the real `ctx.approve` park form
(`request_nowait` + `approval.decided`), `ctx.wait_event`, `max_duration`,
cancel of a parked run, reconcile after a restart (docs/v2.md §2, §6, §11).

Harness: per-test sqlite engine; `_ParkBus` records emits AND dispatches them
inline to exact-name subscribers (the approval engine's `approval.decided`,
the run's `wait_event` subscription); `_NowaitApprovals` is the new-core
engine (`request_nowait` → pending card, `get`, `decide` emitting
`approval.decided`; `request` must never be hit). A "restart" is a fresh
`PlaybookRunner` (its own `ParkService`) + `_ParkBus` on the same engine,
with the card store carried over (the engine persists), followed by
`park.start()` + `park.reconcile()`. Segments are scripted (`ScriptedCodeRun`)
except the one `real_jail` case.
"""

from __future__ import annotations

import asyncio
import json
import types
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from _jail import real_code_run, real_jail, requires_jail
from plugin_playbooks import _ensure_columns
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, PlaybookRun, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.v2 import DbJournalStore
from test_repro_fixplaybooks_lifecycle import _Ctx as _LifecycleCtx
from test_repro_fixplaybooks_runtime import _Tool, _Tools
from test_v2_loop import ScriptedCodeRun, _effect, _error, _pb
from test_v2_resume import _save


# ------------------------------------------------------------------ harness
class _ParkBus:
    """Records `(name, payload)` and dispatches inline to exact-name handlers."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.handlers: dict[str, list] = {}

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))
        for h in list(self.handlers.get(name, [])):
            await h(payload)

    def subscribe(self, name: str, handler, **_flags):
        self.handlers.setdefault(name, []).append(handler)
        return lambda: self.handlers[name].remove(handler)

    def named(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]

    def count(self, name: str) -> int:
        return len(self.handlers.get(name, []))


class _Dec:
    def __init__(self, decision="approved", reason=None, decided_by="owner", request_id=None) -> None:
        self.decision = decision
        self.reason = reason
        self.decided_by = decided_by
        self.request_id = request_id or uuid.uuid4()


class _NowaitApprovals:
    """New-core engine: `request_nowait` mints a pending card; `get` reads
    its status; `decide` flips it and emits `approval.decided`."""

    def __init__(self, bus: _ParkBus, cards: dict | None = None) -> None:
        self.bus = bus
        self.cards: dict[str, types.SimpleNamespace] = cards if cards is not None else {}
        self.nowait_calls: list[dict] = []
        self.request_calls: list[dict] = []
        self.decisions: dict[str, object] = {}
        self.gets: list[str] = []

    async def request(self, **kw):
        self.request_calls.append(kw)
        raise AssertionError("park form must use request_nowait, not request")

    async def request_nowait(self, **kw):
        self.nowait_calls.append(kw)
        rid = uuid.uuid4()
        self.cards[str(rid)] = types.SimpleNamespace(request_id=rid, status="pending")
        return _Dec("pending", request_id=rid)

    async def get(self, request_id):
        self.gets.append(str(request_id))
        return self.cards.get(str(request_id))

    async def decide(self, request_id, decision):
        card = self.cards.get(str(request_id))
        if card is None:
            raise KeyError(str(request_id))
        if card.status != "pending":
            raise ValueError(f"already {card.status}")
        card.status = decision.decision
        self.decisions[str(request_id)] = decision
        await self.bus.emit("approval.decided", {
            "id": str(request_id), "decision": decision.decision,
            "reason": getattr(decision, "reason", None),
            "decided_by": getattr(decision, "decided_by", None),
        })

    def expire(self, request_id) -> None:
        """The engine's sweeper: status flips, nothing is emitted."""
        self.cards[str(request_id)].status = "expired"

    async def record_auto_approval(self, **kw):
        pass


class _Ctx(_LifecycleCtx):
    current_conversation_id = None
    vault = None


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await asyncio.sleep(0.05)
    await engine.dispose()


class _Calls:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fast(self, **_kw):
        self.calls.append("fast")
        return {"ok": True, "which": "fast"}


def _prog(steps, *, value="ok", catch=()):
    """A scripted `run()`: issue `steps` (kind, site, name, args, options) in
    order — one effect per segment — then return `value` (or `value(entries)`).
    A `failed` row whose error type is in `catch` is "caught": the segment
    returns `caught <type>: <message>` and reports the row `handled`."""

    def script(env):
        entries = {e["seq"]: e for e in env["journal"] if e.get("seq")}
        for i, (kind, site, name, args, opts) in enumerate(steps, start=1):
            e = entries.get(i)
            if e is None:
                return _effect(i, site, 1, kind, name, dict(args), **opts)
            if e["status"] in ("done", "failed_handled"):
                continue
            if e["status"] == "failed":
                err = e.get("error") or {}
                if err.get("type") in catch:
                    return {
                        "kind": "return", "handled": [i],
                        "value": f"caught {err.get('type')}: {err.get('message')}",
                    }
                return _error(str(err.get("type") or "Error"), str(err.get("message") or ""))
            return _error("ShimFailure", f"unexpected replay status {e['status']} at seq {i}")
        return {"kind": "return", "value": value(entries) if callable(value) else value}

    return script


APPROVE = ("approve", "approve", None, {"show": {"summary": "x"}}, {})
FAST = ("tool", "fast", "fast", {}, {})


def _wait(name="email.received", timeout=60, filter=None, site="mail"):
    return ("wait_event", site, name, {"name": name, "filter": filter, "timeout": timeout}, {})


class _Env:
    def __init__(self, sf, tools, calls, bus, approvals, ctx, runner) -> None:
        self.sf, self.tools, self.calls = sf, tools, calls
        self.bus, self.approvals, self.ctx, self.runner = bus, approvals, ctx, runner

    async def wait_parked(self, timeout=30.0):
        await _until(lambda: self.bus.named("playbook.run.parked"), timeout)
        await _settle()

    async def wait_completed(self, timeout=30.0):
        await _until(lambda: self.bus.named("playbook.run.completed"), timeout)
        await _settle()
        return self.bus.named("playbook.run.completed")[-1]


def _env(sf, script, *, cards=None, start=True, code_run=None, **runner_kw) -> _Env:
    calls = _Calls()
    tools = {"fast": _Tool(calls.fast)}
    tools["code_run"] = _Tool(code_run or ScriptedCodeRun(script).handler)
    registry = _Tools(**tools)
    bus = _ParkBus()
    approvals = _NowaitApprovals(bus, cards=cards)
    ctx = _Ctx(approvals)
    runner = PlaybookRunner(session_factory=sf, tool_registry=registry, events=bus, context=ctx, **runner_kw)
    if start:
        runner.park.start()
    env = _Env(sf, registry, calls, bus, approvals, ctx, runner)
    env.calls_list = calls.calls  # type: ignore[attr-defined]
    return env


async def _restart(env: _Env, script, *, start=True, reconcile=True, **runner_kw) -> _Env:
    """A fresh runner + bus on the same engine; the card store carries over."""
    env.runner.park.stop()
    new = _env(env.sf, script, cards=env.approvals.cards, start=start, **runner_kw)
    if reconcile:
        await new.runner.park.reconcile()
    return new


async def _until(pred, timeout=30.0, step=0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(step)


async def _settle() -> None:
    """Let the run task unwind and its done-callback pop `_tasks`."""
    for _ in range(3):
        await asyncio.sleep(0.02)


async def _row(sf, run_id) -> PlaybookRun:
    async with sf() as s:
        return await s.get(PlaybookRun, run_id)


async def _journal(sf, run_id) -> list[dict]:
    return await DbJournalStore(sf).read(str(run_id))


async def _steps(sf, run_id) -> list[PlaybookStepRun]:
    async with sf() as s:
        return list((await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run_id)
        )).scalars().all())


async def _park(env: _Env, name: str, source: str = "async def run(ctx, inputs): ...") -> PlaybookRun:
    pb = await _save(env.sf, _pb(name, source))
    run = await env.runner.start_run_background(pb, inputs={})
    await env.wait_parked()
    return run


def _approval_id(row: PlaybookRun) -> str:
    assert row.parked_on["kind"] == "approval"
    return row.parked_on["approval_id"]


# ------------------------------------------------------------------ 1-6 approve
async def test_approve_parks_run_and_raises_card(db):
    env = _env(db, _prog([APPROVE, FAST]))
    run = await _park(env, "approve")
    row = await _row(db, run.id)
    assert row.status == "parked"
    assert row.parked_on["kind"] == "approval"
    assert set(row.parked_on) == {"kind", "approval_id", "since", "due_at"}
    kw = env.approvals.nowait_calls[0]
    assert kw["kind"] == "playbook_effect"
    assert kw["payload"] == {
        "run_id": str(run.id), "seq": 1, "playbook": "approve", "version": run.playbook_version,
    }
    assert kw["requested_by_plugin"] == "plugin-playbooks"
    assert env.approvals.request_calls == []
    assert "fast" not in env.calls.calls
    parked = env.bus.named("playbook.run.parked")
    assert len(parked) == 1
    assert parked[0]["run_id"] == str(run.id)
    assert parked[0]["parked_on"]["approval_id"] == _approval_id(row)
    assert parked[0]["seq"] == 1 and parked[0]["step_id"] == "approve#1"
    entries = await _journal(db, run.id)
    assert entries[1]["status"] == "parked"
    assert entries[1]["parked_on"] == row.parked_on
    assert await DbJournalStore(db).in_flight(str(run.id)) == []
    assert run.id not in env.runner._tasks
    assert env.bus.named("playbook.run.completed") == []
    # the step row stays running while parked
    assert [(s.step_id, s.status) for s in await _steps(db, run.id)] == [("approve#1", "running")]


async def test_approve_decided_resumes_in_process(db):
    env = _env(db, _prog([APPROVE, FAST], value=lambda e: e[1]["result"]))
    run = await _park(env, "approve")
    aid = _approval_id(await _row(db, run.id))
    await env.approvals.decide(aid, _Dec("approved", reason="looks good", decided_by="owner"))
    done = await env.wait_completed()
    assert done["status"] == "done"
    row = await _row(db, run.id)
    assert row.status == "done" and row.parked_on is None
    assert "fast" in env.calls.calls
    entries = await _journal(db, run.id)
    assert entries[1]["status"] == "done"
    assert entries[1]["result"]["approved"] is True
    assert entries[1]["result"]["request_id"] == aid
    assert entries[1]["result"]["reason"] == "looks good"
    assert entries[1]["result"]["decided_by"] == "owner"
    assert entries[2]["kind"] == "tool" and entries[2]["status"] == "done"
    assert env.runner._v2.last_result.value == entries[1]["result"]
    assert {s.step_id: s.status for s in await _steps(db, run.id)} == {"approve#1": "done", "fast#1": "done"}
    assert [p["step_id"] for p in env.bus.named("playbook.step.completed")] == ["approve#1", "fast#1"]
    assert run.id not in env.runner._tasks


async def test_approve_decided_while_down_resumes_on_reconcile(db):
    env = _env(db, _prog([APPROVE, FAST]))
    run = await _park(env, "approve")
    aid = _approval_id(await _row(db, run.id))
    # the process died: a new runner, nothing subscribed yet
    new = await _restart(env, _prog([APPROVE, FAST]), start=False, reconcile=False)
    await new.approvals.decide(aid, _Dec("approved", reason="looks good"))
    assert new.bus.named("playbook.run.completed") == []
    assert (await _row(db, run.id)).status == "parked"
    new.runner.park.start()
    n = await new.runner.park.reconcile()
    assert n == 1
    done = await new.wait_completed()
    assert done["status"] == "done"
    assert "fast" in new.calls.calls and "fast" not in env.calls.calls
    entries = await _journal(db, run.id)
    assert entries[1]["status"] == "done"
    assert entries[1]["result"]["approved"] is True
    assert entries[1]["result"]["request_id"] == aid
    # rebuilt from `get()`: no reason / decided_by on the request object
    assert entries[1]["result"]["reason"] is None
    assert entries[1]["result"]["decided_by"] is None


async def test_approve_decided_after_reconcile_resumes_via_subscription(db):
    env = _env(db, _prog([APPROVE, FAST]))
    run = await _park(env, "approve")
    aid = _approval_id(await _row(db, run.id))
    new = await _restart(env, _prog([APPROVE, FAST]))
    assert new.bus.count("approval.decided") == 1
    assert env.bus.count("approval.decided") == 0  # the old service stopped
    assert (await _row(db, run.id)).status == "parked"
    await new.approvals.decide(aid, _Dec("approved"))
    done = await new.wait_completed()
    assert done["status"] == "done"
    assert "fast" in new.calls.calls
    assert (await _row(db, run.id)).status == "done"


@pytest.mark.parametrize("caught", [True, False])
async def test_approve_rejected_raises_ctx_rejected_catchable(db, caught):
    env = _env(db, _prog([APPROVE, FAST], catch=("Rejected",) if caught else ()))
    run = await _park(env, "approve")
    aid = _approval_id(await _row(db, run.id))
    await env.approvals.decide(aid, _Dec("rejected", reason="no"))
    done = await env.wait_completed()
    row = await _row(db, run.id)
    entries = await _journal(db, run.id)
    assert entries[1]["error"]["type"] == "Rejected"
    assert "no" in entries[1]["error"]["message"]
    assert "fast" not in env.calls.calls
    if caught:
        assert done["status"] == "done" and row.status == "done"
        assert "no" in env.runner._v2.last_result.value
        assert entries[1]["status"] == "failed_handled"
    else:
        assert done["status"] == "failed" and row.status == "failed"
        assert row.error_type == "Rejected"
        assert entries[1]["status"] == "failed"
    assert {s.step_id: s.status for s in await _steps(db, run.id)} == {"approve#1": "failed"}


APPROVE_TTL = ("approve", "approve", None, {"show": {"summary": "x"}}, {"_timeout": 1})


async def test_approve_expired_raises_ctx_approval_expired(db):
    # variant 1: the engine's sweeper flips the card to `expired` (no event);
    # the deadline timer polls `get()` and finds it
    env = _env(db, _prog([APPROVE_TTL, FAST], catch=("ApprovalExpired",)))
    run = await _park(env, "approve")
    t0 = asyncio.get_running_loop().time()
    assert env.approvals.nowait_calls[0]["ttl_seconds"] == 1
    row = await _row(db, run.id)
    aid = _approval_id(row)
    since = datetime.fromisoformat(row.parked_on["since"])
    due = datetime.fromisoformat(row.parked_on["due_at"])
    assert 0.9 <= (due - since).total_seconds() <= 1.1
    # the sweeper flips the card just before the run's deadline timer polls it
    asyncio.get_running_loop().call_later(0.8, env.approvals.expire, aid)
    done = await env.wait_completed(timeout=10)
    assert asyncio.get_running_loop().time() - t0 <= 1.5
    assert done["status"] == "done"
    assert "ApprovalExpired" in env.runner._v2.last_result.value
    entries = await _journal(db, run.id)
    assert entries[1]["status"] == "failed_handled"
    assert entries[1]["error"] == {"type": "ApprovalExpired", "message": "ttl elapsed"}
    assert env.approvals.decisions == {}

    # variant 2: `get()` never leaves `pending` — after the re-polls the
    # service releases the card itself (reason "ttl elapsed") and fails the run
    env2 = _env(db, _prog([APPROVE_TTL, FAST]))
    env2.runner.park.expiry_poll_s = 0.1
    run2 = await _park(env2, "approve-2")
    aid2 = _approval_id(await _row(db, run2.id))
    done2 = await env2.wait_completed(timeout=10)
    assert done2["status"] == "failed"
    row2 = await _row(db, run2.id)
    assert row2.status == "failed" and row2.error_type == "ApprovalExpired"
    assert "ttl elapsed" in row2.error
    dec = env2.approvals.decisions[aid2]
    assert dec.decision == "rejected" and dec.reason == "ttl elapsed" and dec.decided_by == "system"
    assert len(env2.approvals.gets) >= 4
    assert "fast" not in env2.calls.calls
    assert (await _journal(db, run2.id))[1]["status"] == "failed"


# ------------------------------------------------------------------ 7-11 wait_event
async def test_wait_event_parks_until_event(db):
    """v2 twin of test_repro_fixplaybooks_runtime::test_wait_for_event_actually_waits."""
    env = _env(db, _prog([_wait(), FAST]))
    pb = await _save(db, _pb("waiter", "async def run(ctx, inputs): ..."))
    run = await env.runner.start_run_background(pb, inputs={})
    row = await env.runner.wait_for_run(run.id, timeout=2)
    assert "fast" not in env.calls.calls
    assert row.status != "done"
    assert row.status == "parked"
    assert row.parked_on["kind"] == "event" and row.parked_on["event_name"] == "email.received"
    assert set(row.parked_on) == {"kind", "event_name", "since", "due_at"}
    assert env.bus.count("email.received") == 1
    assert env.approvals.nowait_calls == []
    parked = env.bus.named("playbook.run.parked")
    assert parked and parked[0]["parked_on"] == row.parked_on
    entries = await _journal(db, run.id)
    assert entries[1]["kind"] == "wait_event" and entries[1]["status"] == "parked"
    assert run.id not in env.runner._tasks


async def test_wait_event_filter_and_payload(db):
    env = _env(db, _prog([_wait(filter={"account": "a"}), FAST], value=lambda e: e[1]["result"]))
    run = await _park(env, "waiter")
    await env.bus.emit("email.received", {"account": "b", "id": 1})
    await _settle()
    assert env.bus.named("playbook.run.completed") == []
    assert (await _row(db, run.id)).status == "parked"
    assert env.bus.count("email.received") == 1
    payload = {"account": "a", "id": 7}
    await env.bus.emit("email.received", payload)
    done = await env.wait_completed()
    assert done["status"] == "done"
    entries = await _journal(db, run.id)
    assert entries[1]["status"] == "done" and entries[1]["result"] == payload
    assert env.runner._v2.last_result.value == payload
    assert "fast" in env.calls.calls
    assert env.bus.count("email.received") == 0
    assert {s.step_id: s.status for s in await _steps(db, run.id)} == {"mail#1": "done", "fast#1": "done"}


async def test_wait_event_fires_across_restart(db):
    env = _env(db, _prog([_wait(), FAST]))
    run = await _park(env, "waiter")
    new = await _restart(env, _prog([_wait(), FAST]))
    assert new.bus.count("email.received") == 1
    assert env.bus.count("email.received") == 0
    await new.bus.emit("email.received", {"id": 1})
    done = await new.wait_completed()
    assert done["status"] == "done"
    assert (await _row(db, run.id)).status == "done"
    assert "fast" in new.calls.calls and "fast" not in env.calls.calls
    assert new.bus.count("email.received") == 0


@pytest.mark.parametrize("caught", [True, False])
async def test_wait_event_times_out_with_ctx_event_timeout(db, caught):
    env = _env(db, _prog([_wait(timeout=0.2), FAST], catch=("EventTimeout",) if caught else ()))
    run = await _park(env, "waiter")
    t0 = asyncio.get_running_loop().time()
    done = await env.wait_completed(timeout=10)
    assert asyncio.get_running_loop().time() - t0 <= 0.6
    row = await _row(db, run.id)
    entries = await _journal(db, run.id)
    assert entries[1]["error"]["type"] == "EventTimeout"
    assert "email.received" in entries[1]["error"]["message"]
    assert "fast" not in env.calls.calls
    assert env.bus.count("email.received") == 0
    if caught:
        assert done["status"] == "done" and row.status == "done"
        assert entries[1]["status"] == "failed_handled"
    else:
        assert done["status"] == "failed" and row.status == "failed"
        assert row.error_type == "EventTimeout"
        assert entries[1]["status"] == "failed"


async def test_wait_event_deadline_passed_during_downtime(db):
    env = _env(db, _prog([_wait(timeout=0.1), FAST]))
    run = await _park(env, "waiter")
    env.runner.park.stop()  # the process is gone: no timer fires
    await asyncio.sleep(0.3)
    assert (await _row(db, run.id)).status == "parked"
    new = await _restart(env, _prog([_wait(timeout=0.1), FAST]))
    assert new.bus.count("email.received") == 0
    done = await new.wait_completed()
    assert done["status"] == "failed"
    row = await _row(db, run.id)
    assert row.status == "failed" and row.error_type == "EventTimeout"
    assert (await _journal(db, run.id))[1]["status"] == "failed"
    assert "fast" not in new.calls.calls


# ------------------------------------------------------------------ 12-14 cancel / max_duration / re-check
async def test_cancel_parked_run_releases_card_and_subscription(db):
    env = _env(db, _prog([APPROVE, FAST]))
    run = await _park(env, "approve")
    aid = _approval_id(await _row(db, run.id))
    await env.runner.cancel_run(run.id)
    row = await _row(db, run.id)
    assert row.status == "cancelled" and row.parked_on is None
    dec = env.approvals.decisions[aid]
    assert dec.decision == "rejected" and "cancelled" in dec.reason
    completed = env.bus.named("playbook.run.completed")
    assert len(completed) == 1 and completed[0]["status"] == "cancelled"
    # the release's own `approval.decided` echo resumed nothing
    assert env.bus.named("approval.decided") and len(env.bus.named("approval.decided")) == 1
    await asyncio.sleep(0.2)
    assert "fast" not in env.calls.calls
    entries = await _journal(db, run.id)
    assert entries[1]["status"] == "failed" and entries[1]["error"]["type"] == "RunCancelled"
    assert {s.step_id: s.status for s in await _steps(db, run.id)} == {"approve#1": "failed"}
    assert env.runner.park._index == {} and env.runner.park._timers == {}

    env2 = _env(db, _prog([_wait(timeout=0.2), FAST]))
    run2 = await _park(env2, "waiter")
    await env2.runner.cancel_run(run2.id)
    assert env2.bus.count("email.received") == 0
    assert (await _row(db, run2.id)).status == "cancelled"
    await asyncio.sleep(0.4)  # past due_at: no timer left to fire
    row2 = await _row(db, run2.id)
    assert row2.status == "cancelled" and row2.error_type is None
    assert len(env2.bus.named("playbook.run.completed")) == 1
    assert "fast" not in env2.calls.calls


async def test_max_duration_fails_park_loudly(db):
    env = _env(db, _prog([APPROVE, FAST]), max_duration=0.3)
    assert env.runner.park.max_duration == 0.3
    run = await _park(env, "approve")
    t0 = asyncio.get_running_loop().time()
    row = await _row(db, run.id)
    aid = _approval_id(row)
    due = datetime.fromisoformat(row.parked_on["due_at"])
    started = row.started_at.replace(tzinfo=timezone.utc) if row.started_at.tzinfo is None else row.started_at
    assert abs((due - started).total_seconds() - 0.3) < 0.01
    done = await env.wait_completed(timeout=10)
    assert asyncio.get_running_loop().time() - t0 <= 0.8
    row = await _row(db, run.id)
    assert row.status == "failed" and row.error_type == "MaxDurationExceeded"
    assert "max_duration" in row.error and "parked on approval" in row.error
    assert row.failed_at is not None and row.parked_on is None
    assert env.approvals.decisions[aid].decision == "rejected"
    assert done["status"] == "failed" and done["error"] == row.error
    assert "fast" not in env.calls.calls
    entries = await _journal(db, run.id)
    assert entries[1]["status"] == "failed" and entries[1]["error"]["type"] == "MaxDurationExceeded"


async def test_resume_paths_recheck_parked_status(db):
    env = _env(db, _prog([APPROVE, FAST]))
    run = await _park(env, "approve")
    aid = _approval_id(await _row(db, run.id))
    await env.runner.cancel_run(run.id)
    before = list(env.calls.calls)
    n_completed = len(env.bus.named("playbook.run.completed"))
    # a late decision replay and a stray timer: both re-check the row first
    await env.bus.emit("approval.decided", {"id": aid, "decision": "approved", "reason": None, "decided_by": "owner"})
    await env.runner.park.on_due(str(run.id))
    await env.runner.park._resume(str(run.id), done={"approved": True})
    await asyncio.sleep(0.2)
    assert env.calls.calls == before
    assert (await _row(db, run.id)).status == "cancelled"
    assert len(env.bus.named("playbook.run.completed")) == n_completed
    assert run.id not in env.runner._tasks


# ------------------------------------------------------------------ 15-17 tools / sweep
async def test_playbook_status_parked_branch(db):
    env = _env(db, _prog([APPROVE, FAST]))
    run = await _park(env, "approve")
    aid = _approval_id(await _row(db, run.id))
    env2 = _env(db, _prog([_wait(), FAST]))
    run2 = await _park(env2, "waiter")
    pairs = build_tools(db, env.bus, env.runner, env.ctx)
    tools = {td.name: h for td, h in pairs}
    defs = {td.name: td for td, _ in pairs}
    out = json.loads(await tools["playbook_status"](run_id=str(run.id)))
    assert out["status"] == "parked"
    assert out["hint"].startswith(f"parked on approval #{aid} — nothing to poll")
    assert out["parked_on"]["approval_id"] == aid
    out2 = json.loads(await tools["playbook_status"](run_id=str(run2.id)))
    assert out2["hint"].startswith("parked on event 'email.received'")
    assert out2["parked_on"]["kind"] == "event"
    assert "parked" in defs["playbook_status"].description
    assert "parked" in defs["playbook_runs"].parameters["properties"]["status"]["enum"]
    assert "parked" in defs["playbook_cancel"].description


async def test_playbook_run_reports_parked_and_promises_wake(db):
    env = _env(db, _prog([APPROVE, FAST]))

    async def send_muted_message(*a, **kw):
        return None

    env.ctx.send_muted_message = send_muted_message  # wake-capable core
    pb = _pb("approve", "async def run(ctx, inputs): ...")
    pb.agent_autonomy = "agent_may_trigger"
    await _save(db, pb)
    tools = {td.name: h for td, h in build_tools(db, env.bus, env.runner, env.ctx)}
    out = json.loads(await tools["playbook_run"](name="approve", inputs="{}", wait_seconds=10))
    assert out["status"] == "parked", out
    assert "nothing to poll" in out["message"] and "WOKEN" in out["message"]
    assert out["parked_on"]["kind"] == "approval"
    row = await _row(db, uuid.UUID(out["run_id"]))
    assert row.status == "parked" and row.wake_on_complete is True


async def test_sweep_leaves_parked_rows(db):
    env = _env(db, _prog([APPROVE, FAST]))
    run = await _park(env, "approve")
    before = await _row(db, run.id)
    fresh = PlaybookRunner(session_factory=db, tool_registry=env.tools, events=_ParkBus(), context=env.ctx)
    assert await fresh.sweep_orphaned_runs() == 0
    assert await fresh.resume_interrupted_runs() == 0
    after = await _row(db, run.id)
    assert after.status == "parked" and after.parked_on == before.parked_on
    assert after.completed_at is None
    assert fresh._tasks == {}


# ------------------------------------------------------------------ 20 real jail
APPROVE_SRC = (
    'async def run(ctx, inputs):\n'
    '    r = await ctx.approve(show={"summary": "x"})\n'
    '    await ctx.tool("fast")\n'
    '    return r\n'
)


@real_jail
@requires_jail()
async def test_approve_park_resume_real_jail(db, tmp_path):
    env = _env(db, None, code_run=real_code_run(tmp_path))
    pb = await _save(db, _pb("approve", APPROVE_SRC))
    run = await env.runner.start_run_background(pb, inputs={})
    await env.wait_parked(timeout=60)
    row = await _row(db, run.id)
    assert row.status == "parked"
    aid = _approval_id(row)
    assert "fast" not in env.calls.calls
    await env.approvals.decide(aid, _Dec("approved", reason="ok", decided_by="owner"))
    done = await env.wait_completed(timeout=60)
    assert done["status"] == "done"
    assert "fast" in env.calls.calls
    value = env.runner._v2.last_result.value
    assert value["approved"] is True and value["request_id"] == aid
    assert value["reason"] == "ok" and value["decided_by"] == "owner"


# ------------------------------------------------------------------ columns
async def test_parked_on_columns_migrate():
    engine = create_async_engine("sqlite+aiosqlite://")
    tables = ("playbook_runs", "playbook_journal")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for table in tables:
                await conn.execute(text(f"ALTER TABLE {table} DROP COLUMN parked_on"))

        def cols(sync_conn):
            return {t: {c["name"] for c in inspect(sync_conn).get_columns(t)} for t in tables}

        async with engine.begin() as conn:
            before = await conn.run_sync(cols)
        assert all("parked_on" not in before[t] for t in tables)
        await _ensure_columns(engine)
        async with engine.begin() as conn:
            after = await conn.run_sync(cols)
        assert all(after[t] - before[t] == {"parked_on"} for t in tables)
        await _ensure_columns(engine)
        async with engine.begin() as conn:
            again = await conn.run_sync(cols)
        assert again == after
    finally:
        await engine.dispose()
