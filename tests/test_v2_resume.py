"""plans/032 phase 06 — durable journal table, write-ahead `in_flight` rows,
resume on `on_server_ready`, `OutcomeUnknown` (docs/v2.md §6).

A "process death" is `_ProcessDied(BaseException)`: nothing in the loop or
`_drive_run` catches `BaseException`, so the run row stays `running` and the
journal keeps whatever was committed — exactly what a killed process leaves.
A "restart" is a fresh `PlaybookRunner` on the same session factory (its
`_tasks` is empty) followed by `resume_interrupted_runs()`.

Scripted tests fake `code_run`; `real_jail` tests drive the real shim (the
`try`/`except ctx.OutcomeUnknown` and `gather` semantics are `run()` code
semantics a scripted fake would only restate). Skipped without a jail.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import types
import uuid

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from _jail import real_code_run, real_jail, requires_jail
from plugin_playbooks import PlaybooksPlugin
from plugin_playbooks.models import (
    Base, Playbook, PlaybookJournal, PlaybookRun, PlaybookStepRun, PlaybookVersion,
)
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.v2 import DbJournalStore, MemoryJournalStore
from plugin_playbooks.v2.journal import make_effect_entry, make_entry0
from plugin_playbooks.versioning import shim_playbook
from test_manifest_flow import _Agent
from test_v2_loop import (
    ScriptedCodeRun, _Bus, _Ctx, _Tool, _Tools, _Vault, _effect, _pb,
)


class _ProcessDied(BaseException):
    """The simulated kill — never caught by the runtime."""


# ------------------------------------------------------------------ harness
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
async def db_file(tmp_path):
    """File-backed sqlite with the default (per-session) pool: every session
    gets its own connection, as in PostgreSQL. `db` (in-memory) is a
    StaticPool — one connection for all sessions — so two runs resumed
    concurrently interleave one session's ROLLBACK with the other's
    uncommitted `append_in_flight` insert (plugin/06 "Learned"). Use this for
    any test that drives more than one run task at once."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/journal.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await asyncio.sleep(0.05)
    await engine.dispose()


async def _save(sf, pb: Playbook, *, versions: dict[int, str] | None = None) -> Playbook:
    """Persist a playbook AND the version row(s) a resume pins on
    (`_create_run` stamps `playbook_version = live_version or version`)."""
    async with sf() as s:
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
        rows = versions or {pb.live_version or pb.version: pb.code}
        for n, code in rows.items():
            s.add(PlaybookVersion(
                playbook_id=pb.id, version=n, definition=pb.definition, code=code,
                author="owner", message=f"v{n}",
            ))
        await s.commit()
    return pb


def _runner(sf, tools, *, events=None, agent=None, context=None) -> tuple[PlaybookRunner, _Bus]:
    bus = events or _Bus()
    runner = PlaybookRunner(
        session_factory=sf, tool_registry=tools, events=bus, agent=agent, context=context,
    )
    return runner, bus


def _restart(sf, tools, **kw) -> tuple[PlaybookRunner, _Bus]:
    """A fresh runner on the same DB: what plugin load after a death builds."""
    return _runner(sf, tools, **kw)


async def _row(sf, run_id) -> PlaybookRun:
    async with sf() as s:
        return await s.get(PlaybookRun, run_id)


async def _steps(sf, run_id) -> list[PlaybookStepRun]:
    async with sf() as s:
        return list((await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run_id)
            .order_by(PlaybookStepRun.started_at, PlaybookStepRun.id)
        )).scalars().all())


async def _journal(sf, run_id) -> list[dict]:
    return await DbJournalStore(sf).read(str(run_id))


async def _until(pred, timeout=60.0, step=0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        assert loop.time() < deadline, "condition not reached in time"
        await asyncio.sleep(step)


async def _dead(runner, run_id, timeout=30.0) -> None:
    """Wait for the run task to end by `_ProcessDied` (the row stays running)."""
    task = runner._tasks.get(run_id)
    if task is not None:
        await asyncio.wait([task], timeout=timeout)
        assert task.done()
        assert isinstance(task.exception(), _ProcessDied)
    assert (await _row(runner._sf, run_id)).status == "running"


def _real_tools(tmp_path, **tools) -> _Tools:
    t = _Tools(**{k: _Tool(v) for k, v in tools.items()})
    t.add("code_run", real_code_run(tmp_path))
    return t


class _Counter:
    """A tool that counts its calls and optionally dies (or gates) first."""

    def __init__(self, name: str, *, die_on: int | None = None, gate: asyncio.Event | None = None):
        self.name = name
        self.calls: list[dict] = []
        self.die_on = die_on
        self.gate = gate
        self.started = asyncio.Event()
        self.die_after_gate = False

    async def __call__(self, **kw):
        self.calls.append(kw)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
            if self.die_after_gate:
                raise _ProcessDied()
        if self.die_on is not None and len(self.calls) == self.die_on:
            raise _ProcessDied()
        return {"t": self.name, "n": len(self.calls), **kw}


THREE_TOOLS = '''async def run(ctx, inputs):
    a = await ctx.tool("t1", k=1)
    b = await ctx.tool("t2", k=2)
    c = await ctx.tool("t3", k=3)
    return {"a": a, "b": b, "c": c}
'''


def _three_script(arm: dict):
    """The shim of THREE_TOOLS, scripted: effect seq = journal length; dies
    when asked for segment 3 (journal rows 0-2 present) while armed."""

    def script(env):
        n = len(env["journal"])
        if n == 3 and arm.get("die"):
            raise _ProcessDied()
        if n <= 3:
            return _effect(n, f"t{n}", 1, "tool", f"t{n}", {"k": n})
        return {"kind": "return", "value": "ok"}

    return script


# ------------------------------------------------------------------ Step 1
async def test_journal_table_created_idempotently():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        for _ in range(2):  # the on_load loop (__init__.py), twice
            async with engine.begin() as conn:
                for table in Base.metadata.sorted_tables:
                    await conn.run_sync(table.create, checkfirst=True)

        def shape(sync_conn):
            insp = inspect(sync_conn)
            return (
                "playbook_journal" in insp.get_table_names(),
                insp.get_pk_constraint("playbook_journal")["constrained_columns"],
                [(i["name"], bool(i["unique"]), list(i["column_names"]))
                 for i in insp.get_indexes("playbook_journal")],
            )

        async with engine.begin() as conn:
            present, pk, indexes = await conn.run_sync(shape)
        assert present
        assert pk == ["run_id", "seq"]
        assert ("ux_playbook_journal_idem", True, ["idempotency_key"]) in indexes
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ Step 2
def _strip_times(journal: list[dict]) -> list[dict]:
    out = []
    for e in journal:
        e = dict(e)
        for k in ("started_at", "ended_at"):
            e.pop(k, None)
        out.append(e)
    return out


async def _run_row(sf, run_id) -> Playbook:
    pb = await _save(sf, _pb("store", 'async def run(ctx, inputs):\n    return 1\n'))
    async with sf() as s:
        s.add(PlaybookRun(id=run_id, playbook_id=pb.id, playbook_version=1, inputs={}, status="running"))
        await s.commit()
    return pb


async def test_db_journal_store_matches_memory_store(db):
    rid = uuid.uuid4()
    await _run_row(db, rid)
    mem = MemoryJournalStore(keep_completed=True)
    dbs = DbJournalStore(db)
    e0 = make_entry0(
        hash_seed=5, inputs={"n": 1}, playbook="store", version=1, max_effects=200,
        code_sha256="cd" * 32,
    )
    for store in (mem, dbs):
        assert await store.journaled([rid, uuid.uuid4()]) == set()
        assert await store.entry0(str(rid)) is None
        await store.start(str(rid), dict(e0))
        s1 = await store.append_in_flight(str(rid), make_effect_entry(
            run_id=str(rid), seq=0, kind="tool", id="post", occurrence=1, name="http",
            args={"token": "vault:api_key", "n": 1},
        ))
        assert s1 == 1
        assert [e["seq"] for e in await store.in_flight(str(rid))] == [1]
        await store.complete(str(rid), 1, {"v": 1}, [{"n": 1, "error": None, "ms": 2}], 2, extra={"cost_cents": 3})
        s2 = await store.append_in_flight(str(rid), make_effect_entry(
            run_id=str(rid), seq=0, kind="llm", id="a", occurrence=1, name=None, args={"prompt": "p"},
        ))
        await store.fail(str(rid), s2, "EffectError", "boom", [{"n": 1, "error": "EffectError: boom", "ms": 1}],
                         extra={"transcript": [{"kind": "tool", "label": "t"}]})
        s3 = await store.append_in_flight(str(rid), make_effect_entry(
            run_id=str(rid), seq=0, kind="subtask", id="child", occurrence=1, name="child", args={"inputs": {}},
        ))
        await store.mark_unknown(str(rid), s3, "outcome unknown — the server restarted while effect child#1 was in flight")
        s4 = await store.append_in_flight(str(rid), make_effect_entry(
            run_id=str(rid), seq=0, kind="agent", id="ask", occurrence=1, name=None, args={"prompt": "q"},
        ))
        assert (s2, s3, s4) == (2, 3, 4)
        assert [e["seq"] for e in await store.in_flight(str(rid))] == [4]
        await store.mark_handled(str(rid), [2, 3])
        assert await store.journaled([rid, uuid.uuid4()]) == {rid}
        assert (await store.entry0(str(rid)))["code_sha256"] == "cd" * 32
        await store.drop(str(rid))  # durable: a no-op; memory(keep_completed): kept

    j_mem = await mem.read(str(rid))
    j_db = await dbs.read(str(rid))
    assert len(j_db) == 5
    assert _strip_times(j_mem) == _strip_times(j_db)
    assert [e["status"] for e in j_db[1:]] == ["done", "failed_handled", "timed_out_unknown", "in_flight"]
    assert j_db[1]["cost_cents"] == 3 and j_db[2]["transcript"] == [{"kind": "tool", "label": "t"}]
    assert j_db[3]["error"]["type"] == "OutcomeUnknown"
    # timestamps of effect rows come back as ISO-8601 UTC strings on both
    assert j_db[1]["started_at"].endswith("+00:00") and j_db[1]["ended_at"].endswith("+00:00")
    assert j_db[4]["ended_at"] is None
    with pytest.raises(KeyError):
        await dbs.read(str(uuid.uuid4()))
    with pytest.raises(KeyError):
        await dbs.append_in_flight(str(uuid.uuid4()), make_effect_entry(
            run_id="x", seq=0, kind="tool", id="t", occurrence=1, name="t", args={},
        ))


async def test_journal_rows_store_raw_vault_refs(db):
    seen: list[dict] = []

    async def http(**kw):
        seen.append(kw)
        return {"ok": True}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "resp", 1, "tool", "http", {"headers": {"x-api-key": "vault:my_key"}})
        return {"kind": "return", "value": "ok"}

    fake = ScriptedCodeRun(script)
    tools = _Tools(http=_Tool(http), code_run=_Tool(fake.handler))
    vault = _Vault({"my_key": "s3cret-value"})
    runner, _ = _runner(db, tools, context=_Ctx(vault=vault))
    pb = await _save(db, _pb("vault", 'async def run(ctx, inputs):\n    resp = await ctx.tool("http", headers={"x-api-key": "vault:my_key"})\n    return "ok"\n'))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=10)
    assert row.status == "done", (row.error, row.traceback)
    assert seen == [{"headers": {"x-api-key": "s3cret-value"}}]
    assert vault.reads == ["my_key"]
    async with db() as s:
        rows = list((await s.execute(
            select(PlaybookJournal).where(PlaybookJournal.run_id == run.id).order_by(PlaybookJournal.seq)
        )).scalars().all())
    assert [r.seq for r in rows] == [0, 1]
    assert rows[1].args == {"headers": {"x-api-key": "vault:my_key"}}
    assert rows[1].call_site_id == "resp" and rows[1].name == "http" and rows[1].status == "done"
    dumped = json.dumps([{"args": r.args, "result": r.result, "error": r.error} for r in rows], default=str)
    assert "s3cret-value" not in dumped
    # row 0 carries entry 0 (incl. the code pin) — the v2 marker
    assert rows[0].kind == "run" and rows[0].status == "done"
    assert rows[0].args["code_sha256"] == hashlib.sha256(pb.code.encode()).hexdigest()
    assert rows[0].args["inputs"] == {} and rows[0].args["playbook"] == "vault"
    assert rows[0].idempotency_key == f"{run.id}:0" and rows[1].idempotency_key == f"{run.id}:1"


# ------------------------------------------------------------------ Step 3
async def test_in_flight_row_committed_before_handler_runs(db):
    seen: dict = {}

    async def probe(**kw):
        # a SEPARATE session: only a committed row is visible here
        async with db() as s:
            rows = list((await s.execute(
                select(PlaybookJournal).where(PlaybookJournal.seq == 1)
            )).scalars().all())
        seen["rows"] = [(str(r.run_id), r.kind, r.name, r.status, r.args, r.idempotency_key) for r in rows]
        return {"ok": True}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "probe", 1, "tool", "probe", {"x": 1})
        return {"kind": "return", "value": "ok"}

    fake = ScriptedCodeRun(script)
    tools = _Tools(probe=_Tool(probe), code_run=_Tool(fake.handler))
    runner, _ = _runner(db, tools)
    assert isinstance(runner._v2.journal, DbJournalStore)  # the default store
    pb = await _save(db, _pb("wa", 'async def run(ctx, inputs):\n    await ctx.tool("probe", x=1)\n    return "ok"\n'))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=10)
    assert row.status == "done", (row.error, row.traceback)
    assert seen["rows"] == [(str(run.id), "tool", "probe", "in_flight", {"x": 1}, f"{run.id}:1")]
    assert (await _journal(db, run.id))[1]["status"] == "done"


# ------------------------------------------------------------------ Step 4
async def test_v1_orphans_swept_v2_running_rows_not(db):
    arm = {"die": True}
    t1, t2, t3 = _Counter("t1"), _Counter("t2"), _Counter("t3")
    fake_a = ScriptedCodeRun(_three_script(arm))
    tools_a = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_a.handler))
    runner_a, _ = _runner(db, tools_a)
    pb = await _save(db, _pb("v2pb", THREE_TOOLS))
    v2_run = await runner_a.start_run_background(pb, inputs={})
    await _dead(runner_a, v2_run.id)
    # a v1 row: no journal, nothing driving it
    v1_pb = await _save(db, Playbook(
        name="v1pb", display_name="v1pb", status="enabled",
        definition={"name": "v1pb", "steps": [{"id": "s1", "kind": "tool_call", "tool": "t1", "args": {}}]},
    ))
    async with db() as s:
        v1_run = PlaybookRun(playbook_id=v1_pb.id, playbook_version=1, inputs={}, status="running")
        s.add(v1_run)
        await s.commit()
        v1_id = v1_run.id

    arm["die"] = False
    fake_b = ScriptedCodeRun(_three_script(arm))
    tools_b = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_b.handler))
    runner_b, bus_b = _restart(db, tools_b)
    assert await runner_b.sweep_orphaned_runs() == 1
    v1_row = await _row(db, v1_id)
    assert v1_row.status == "failed" and v1_row.error_type == "Interrupted"
    assert "interrupted" in v1_row.error
    assert (await _row(db, v2_run.id)).status == "running"
    assert await runner_b.sweep_orphaned_runs() == 0
    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(v2_run.id, timeout=10)
    assert row.status == "done", (row.error, row.traceback)
    assert [len(t.calls) for t in (t1, t2, t3)] == [1, 1, 1]
    assert [p["run_id"] for p in bus_b.named("playbook.run.completed")] == [str(v2_run.id)]


# ------------------------------------------------------------------ Step 6 (headline)
async def test_kill_mid_run_restart_resumes_with_same_journal_prefix(db):
    arm = {"die": True}
    t1, t2, t3 = _Counter("t1"), _Counter("t2"), _Counter("t3")
    fake_a = ScriptedCodeRun(_three_script(arm))
    tools_a = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_a.handler))
    runner_a, bus_a = _runner(db, tools_a)
    pb = await _save(db, _pb("three", THREE_TOOLS))
    run = await runner_a.start_run_background(pb, inputs={"n": 7})
    await _dead(runner_a, run.id)
    assert len(fake_a.calls) == 3  # segments 1, 2 and the fatal request for 3
    before = await _journal(db, run.id)
    assert [e["seq"] for e in before] == [0, 1, 2]
    assert [e["status"] for e in before[1:]] == ["done", "done"]
    assert before[0]["inputs"] == {"n": 7}
    assert before[0]["code_sha256"] == hashlib.sha256(THREE_TOOLS.encode()).hexdigest()
    assert bus_a.named("playbook.run.completed") == []

    arm["die"] = False
    fake_b = ScriptedCodeRun(_three_script(arm))
    tools_b = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_b.handler))
    runner_b, bus_b = _restart(db, tools_b)
    assert await runner_b.resume_interrupted_runs() == 1
    assert run.id in runner_b._tasks
    assert runner_b._tasks[run.id].get_name() == f"playbook-run-{run.id}"
    row = await runner_b.wait_for_run(run.id, timeout=10)
    assert row.status == "done", (row.error, row.traceback)
    assert row.error is None and row.error_type is None
    after = await _journal(db, run.id)
    assert [e["seq"] for e in after] == [0, 1, 2, 3]
    assert after[:3] == before  # field-for-field, idempotency_key and started_at included
    assert after[3]["kind"] == "tool" and after[3]["name"] == "t3" and after[3]["status"] == "done"
    assert after[3]["idempotency_key"] == f"{run.id}:3" and after[3]["args"] == {"k": 3}
    assert [len(t.calls) for t in (t1, t2, t3)] == [1, 1, 1]
    # the resumed segments saw the same envelope identity as the first process
    assert fake_b.calls[0]["input_json"]["source"] == THREE_TOOLS
    assert fake_b.calls[0]["input_json"]["hash_seed"] == before[0]["hash_seed"]
    assert [len(c["input_json"]["journal"]) for c in fake_b.calls] == [3, 4]
    done = bus_b.named("playbook.run.completed")
    assert len(done) == 1 and done[0]["status"] == "done" and done[0]["run_id"] == str(run.id)
    assert done[0]["playbook_version"] == 1
    assert runner_b._v2.last_result.value == "ok"
    steps = await _steps(db, run.id)
    assert [(s.step_id, s.status) for s in steps] == [("t1#1", "done"), ("t2#1", "done"), ("t3#1", "done")]
    assert run.id not in runner_b._tasks
    assert await runner_b.resume_interrupted_runs() == 0


async def test_interrupted_candidate_run_stamps_completion_wake_before_resume(db):
    arm = {"die": True}
    t1, t2, t3 = _Counter("t1"), _Counter("t2"), _Counter("t3")
    tools_a = _Tools(
        t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3),
        code_run=_Tool(ScriptedCodeRun(_three_script(arm)).handler),
    )
    runner_a, _ = _runner(db, tools_a)
    pb = await _save(db, _pb("candidate-restart", THREE_TOOLS))
    run = await runner_a.start_run_background(
        pb, inputs={}, trigger="agent-candidate", is_test=True,
    )
    await _dead(runner_a, run.id)
    origin = uuid.uuid4()
    async with db() as s:
        row = await s.get(PlaybookRun, run.id)
        row.conversation_id = origin
        await s.commit()
    assert (await _row(db, run.id)).wake_on_complete is False

    arm["die"] = False
    tools_b = _Tools(
        t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3),
        code_run=_Tool(ScriptedCodeRun(_three_script(arm)).handler),
    )
    runner_b, bus_b = _restart(db, tools_b)
    assert await runner_b.resume_interrupted_runs() == 1
    assert (await _row(db, run.id)).wake_on_complete is True
    done = await runner_b.wait_for_run(run.id, timeout=10)
    assert done.status == "done"
    events = bus_b.named("playbook.run.completed")
    assert len(events) == 1
    assert events[0]["is_test"] is True
    assert events[0]["wake_on_complete"] is True
    assert events[0]["conversation_id"] == str(origin)


class _DyingCodeRun:
    """Wraps the real `code_run` handler: raises `_ProcessDied` on invocation
    `die_on` (before spawning) while armed — the same death point as the
    scripted twin (asked for segment 3, seqs 0-2 done)."""

    def __init__(self, inner, die_on: int) -> None:
        self.inner = inner
        self.die_on = die_on
        self.armed = True
        self.n = 0

    async def __call__(self, **kw):
        self.n += 1
        if self.armed and self.n == self.die_on:
            raise _ProcessDied()
        return await self.inner(**kw)


THREE_REAL = '''async def run(ctx, inputs):
    a = await ctx.tool("alpha", n=inputs["n"])
    b = await ctx.tool("beta", prev=a["n"])
    c = await ctx.tool("gamma", prev=b["n"])
    return {"a": a, "b": b, "c": c}
'''


@real_jail
@requires_jail()
async def test_kill_mid_run_restart_resumes_with_same_journal_prefix_real_jail(db, tmp_path):
    alpha, beta, gamma = _Counter("alpha"), _Counter("beta"), _Counter("gamma")
    dying = _DyingCodeRun(real_code_run(tmp_path), die_on=3)
    tools = _Tools(alpha=_Tool(alpha), beta=_Tool(beta), gamma=_Tool(gamma))
    tools.add("code_run", dying)
    runner_a, bus_a = _runner(db, tools)
    pb = await _save(db, _pb("three-real", THREE_REAL))
    run = await runner_a.start_run_background(pb, inputs={"n": 7})
    await _dead(runner_a, run.id, timeout=90)
    before = await _journal(db, run.id)
    assert [e["seq"] for e in before] == [0, 1, 2]
    assert [e["status"] for e in before[1:]] == ["done", "done"]
    assert [e["name"] for e in before[1:]] == ["alpha", "beta"]
    assert bus_a.named("playbook.run.completed") == []

    dying.armed = False
    runner_b, bus_b = _restart(db, tools)
    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(run.id, timeout=90)
    assert row.status == "done", (row.error, row.traceback)
    after = await _journal(db, run.id)
    assert [e["seq"] for e in after] == [0, 1, 2, 3]
    assert after[:3] == before
    assert after[3]["name"] == "gamma" and after[3]["status"] == "done"
    assert after[3]["args"] == {"prev": 1}
    assert [len(t.calls) for t in (alpha, beta, gamma)] == [1, 1, 1]
    assert dying.n == 5  # 2 real segments + the fatal request + 2 resumed segments
    done = bus_b.named("playbook.run.completed")
    assert len(done) == 1 and done[0]["status"] == "done"
    value = runner_b._v2.last_result.value
    assert value["a"]["t"] == "alpha" and value["c"]["prev"] == 1


V2_SRC = '''async def run(ctx, inputs):
    a = await ctx.tool("t1", k=1)
    b = await ctx.tool("t2", k=2)
    c = await ctx.tool("t3", k=3)
    return "v2"
'''
V3_SRC = V2_SRC.replace('return "v2"', 'return "v3"')


async def test_resume_uses_pinned_version_code(db):
    arm = {"die": True}
    t1, t2, t3 = _Counter("t1"), _Counter("t2"), _Counter("t3")
    fake_a = ScriptedCodeRun(_three_script(arm))
    tools_a = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_a.handler))
    # `playbooks.code` holds v2 (live); v3 is the candidate the run executes
    pb = _pb("pinned", V2_SRC)
    pb.version, pb.live_version, pb.candidate_version = 3, 2, 3
    pb = await _save(db, pb, versions={2: V2_SRC, 3: V3_SRC})
    async with db() as s:
        row3 = (await s.execute(select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == pb.id, PlaybookVersion.version == 3,
        ))).scalar_one()
    runner_a, _ = _runner(db, tools_a)
    run = await runner_a.start_run_background(shim_playbook(pb, row3), inputs={})
    await _dead(runner_a, run.id)
    assert (await _row(db, run.id)).playbook_version == 3
    assert fake_a.calls[0]["input_json"]["source"] == V3_SRC
    assert (await _journal(db, run.id))[0]["code_sha256"] == hashlib.sha256(V3_SRC.encode()).hexdigest()

    arm["die"] = False
    fake_b = ScriptedCodeRun(_three_script(arm))
    tools_b = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_b.handler))
    runner_b, bus_b = _restart(db, tools_b)
    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(run.id, timeout=10)
    assert row.status == "done", (row.error, row.traceback)
    assert {c["input_json"]["source"] for c in fake_b.calls} == {V3_SRC}
    assert {c["input_json"]["version"] for c in fake_b.calls} == {3}
    assert bus_b.named("playbook.run.completed")[0]["playbook_version"] == 3
    assert runner_b._v2.last_result.value == "ok"  # the scripted twin's return
    assert (await _row(db, run.id)).playbook_version == 3


async def test_code_edited_under_run_is_divergence(db):
    arm = {"die": True}
    t1, t2, t3 = _Counter("t1"), _Counter("t2"), _Counter("t3")
    fake_a = ScriptedCodeRun(_three_script(arm))
    tools_a = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_a.handler))
    runner_a, _ = _runner(db, tools_a)
    pb = await _save(db, _pb("edited", THREE_TOOLS))
    run = await runner_a.start_run_background(pb, inputs={})
    await _dead(runner_a, run.id)
    async with db() as s:  # the pinned row's code changes under the run
        vrow = (await s.execute(select(PlaybookVersion).where(PlaybookVersion.playbook_id == pb.id))).scalar_one()
        vrow.code = THREE_TOOLS.replace('k=3', 'k=4')
        await s.commit()

    arm["die"] = False
    fake_b = ScriptedCodeRun(_three_script(arm))
    tools_b = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_b.handler))
    runner_b, bus_b = _restart(db, tools_b)
    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(run.id, timeout=10)
    assert row.status == "failed"
    assert row.error_type == "JournalDivergence"
    assert "code edited under a run" in row.error and "edited" in row.error
    assert row.failed_at is not None and row.completed_at is not None
    assert fake_b.calls == []  # no jail spawn
    assert [len(t.calls) for t in (t1, t2, t3)] == [1, 1, 0]  # nothing re-executed
    assert [e["status"] for e in (await _journal(db, run.id))[1:]] == ["done", "done"]
    done = bus_b.named("playbook.run.completed")
    assert len(done) == 1 and done[0]["status"] == "failed"


async def test_resume_missing_version_row_fails_loud(db):
    arm = {"die": True}
    t1, t2, t3 = _Counter("t1"), _Counter("t2"), _Counter("t3")
    fake_a = ScriptedCodeRun(_three_script(arm))
    tools_a = _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_a.handler))
    runner_a, _ = _runner(db, tools_a)
    pb = await _save(db, _pb("rowless", THREE_TOOLS))
    run = await runner_a.start_run_background(pb, inputs={})
    await _dead(runner_a, run.id)
    async with db() as s:
        vrow = (await s.execute(select(PlaybookVersion).where(PlaybookVersion.playbook_id == pb.id))).scalar_one()
        await s.delete(vrow)
        await s.commit()
    runner_b, _ = _restart(db, _Tools(code_run=_Tool(ScriptedCodeRun(_three_script({})).handler)))
    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(run.id, timeout=10)
    assert row.status == "failed" and row.error_type == "VersionMissing"
    assert "version 1" in row.error and "rowless" in row.error


GATED_THEN_RETURN = '''async def run(ctx, inputs):
    a = await ctx.tool("t1", k=1)
    b = await ctx.tool("slow")
    return "ok"
'''


def _gated_script(arm: dict):
    def script(env):
        n = len(env["journal"])
        if n == 1:
            return _effect(1, "a", 1, "tool", "t1", {"k": 1})
        if n == 2:
            if arm.get("die"):
                raise _ProcessDied()
            return _effect(2, "b", 1, "tool", "slow", {})
        return {"kind": "return", "value": "ok"}

    return script


async def test_cancel_run_works_on_resumed_run(db):
    arm = {"die": True}
    gate = asyncio.Event()
    t1, slow = _Counter("t1"), _Counter("slow", gate=gate)
    fake_a = ScriptedCodeRun(_gated_script(arm))
    runner_a, _ = _runner(db, _Tools(t1=_Tool(t1), slow=_Tool(slow), code_run=_Tool(fake_a.handler)))
    pb = await _save(db, _pb("cancelme", GATED_THEN_RETURN))
    run = await runner_a.start_run_background(pb, inputs={})
    await _dead(runner_a, run.id)

    arm["die"] = False
    fake_b = ScriptedCodeRun(_gated_script(arm))
    runner_b, bus_b = _restart(db, _Tools(t1=_Tool(t1), slow=_Tool(slow), code_run=_Tool(fake_b.handler)))
    assert await runner_b.resume_interrupted_runs() == 1
    await asyncio.wait_for(slow.started.wait(), 10)  # the resumed run is a live task, blocked in `slow`
    task = runner_b._tasks[run.id]
    assert not task.done()
    await runner_b.cancel_run(run.id)  # the task path, not the DB fallback
    row = await runner_b.wait_for_run(run.id, timeout=10)
    assert task.cancelled() or task.done()
    assert row.status == "cancelled"
    j = await _journal(db, run.id)
    assert j[2]["status"] == "failed" and j[2]["error"]["type"] == "RunCancelled"
    done = bus_b.named("playbook.run.completed")
    assert len(done) == 1 and done[0]["status"] == "cancelled"
    gate.set()


# ------------------------------------------------------------------ Step 7
async def test_on_server_ready_resumes_and_returns_count(db, caplog):
    arm = {"die": True}
    t1, t2, t3 = _Counter("t1"), _Counter("t2"), _Counter("t3")
    fake_a = ScriptedCodeRun(_three_script(arm))
    runner_a, _ = _runner(db, _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_a.handler)))
    pb = await _save(db, _pb("ready", THREE_TOOLS))
    run = await runner_a.start_run_background(pb, inputs={})
    await _dead(runner_a, run.id)

    arm["die"] = False
    fake_b = ScriptedCodeRun(_three_script(arm))
    runner_b, _ = _restart(db, _Tools(t1=_Tool(t1), t2=_Tool(t2), t3=_Tool(t3), code_run=_Tool(fake_b.handler)))
    fake_plugin = types.SimpleNamespace(_runner=runner_b)
    with caplog.at_level(logging.INFO, logger="plugin_playbooks"):
        await asyncio.wait_for(PlaybooksPlugin.on_server_ready(fake_plugin), 2)  # returns after spawning
    assert "playbooks: resumed 1 interrupted v2 run(s)" in caplog.text
    assert run.id in runner_b._tasks
    row = await runner_b.wait_for_run(run.id, timeout=10)
    assert row.status == "done", (row.error, row.traceback)
    with caplog.at_level(logging.INFO, logger="plugin_playbooks"):
        await PlaybooksPlugin.on_server_ready(fake_plugin)
    assert "playbooks: resumed 0 interrupted v2 run(s)" in caplog.text


# ------------------------------------------------------------------ Step 9 (scripted)
RECOVER_SRC = '''async def run(ctx, inputs):
    try:
        await ctx.tool("slow")
    except ctx.OutcomeUnknown:
        return "recovered"
    return "ok"
'''


def _recover_script(env):
    """The shim of RECOVER_SRC, scripted: an unknown row is caught."""
    j = env["journal"]
    if len(j) == 1:
        return _effect(1, "slow", 1, "tool", "slow", {})
    if j[1]["status"] in ("in_flight", "timed_out_unknown"):
        return {"kind": "return", "value": "recovered", "handled": [1]}
    return {"kind": "return", "value": "ok"}


async def test_interrupted_run_survives_restart_instead_of_failing(db):
    """v2 twin of tests/test_repro_fixplaybooks_runtime.py:98-122 (the v1
    original stays red): a run in flight when the process dies is resumed
    by the next process, not stamped failed."""
    gate = asyncio.Event()
    slow = _Counter("slow", gate=gate)
    tools_a = _Tools(slow=_Tool(slow), code_run=_Tool(ScriptedCodeRun(_recover_script).handler))
    runner_a, _ = _runner(db, tools_a)
    pb = await _save(db, _pb("long-job", RECOVER_SRC))
    run = await runner_a.start_run_background(pb, inputs={})
    await asyncio.wait_for(slow.started.wait(), 10)  # step 1 is genuinely in flight

    # "Restart": a fresh runner on the same DB (its _tasks is empty, exactly
    # like plugin load after a process death) runs its on-load sweep.
    tools_b = _Tools(slow=_Tool(slow), code_run=_Tool(ScriptedCodeRun(_recover_script).handler))
    runner_b, bus_b = _restart(db, tools_b)
    await runner_b.sweep_orphaned_runs()
    row = await _row(db, run.id)
    assert row.status != "failed", (
        "restart killed the run: sweep_orphaned_runs stamped it 'failed'"
    )
    assert row.status == "running"
    # the old process dies for real while `slow` is in flight
    slow.die_after_gate = True
    gate.set()
    await _dead(runner_a, run.id)
    assert (await _journal(db, run.id))[1]["status"] == "in_flight"

    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(run.id, timeout=10)
    assert row.status == "done", (row.error, row.traceback)
    assert runner_b._v2.last_result.value == "recovered"
    assert len(slow.calls) == 1  # never re-executed
    j = await _journal(db, run.id)
    assert j[1]["status"] == "timed_out_unknown"
    assert j[1]["error"] == {
        "type": "OutcomeUnknown",
        "message": "outcome unknown — the server restarted while effect slow#1 was in flight",
    }
    steps = await _steps(db, run.id)
    assert steps[0].status == "failed" and "OutcomeUnknown" in steps[0].error and "slow#1" in steps[0].error
    assert bus_b.named("playbook.run.completed")[0]["status"] == "done"


async def test_resumed_completion_payload_matches_uninterrupted_run(db):
    gate = asyncio.Event()
    t1, slow = _Counter("t1"), _Counter("slow", gate=gate)
    # control: gated so `wake_on_complete` can be stamped before completion
    arm = {"die": False}
    runner_a, bus_a = _runner(db, _Tools(t1=_Tool(t1), slow=_Tool(slow), code_run=_Tool(ScriptedCodeRun(_gated_script(arm)).handler)))
    pb = await _save(db, _pb("payload", GATED_THEN_RETURN))
    control = await runner_a.start_run_background(pb, inputs={})
    await asyncio.wait_for(slow.started.wait(), 10)
    async with db() as s:
        (await s.get(PlaybookRun, control.id)).wake_on_complete = True
        await s.commit()
    gate.set()
    assert (await runner_a.wait_for_run(control.id, timeout=10)).status == "done"
    payload_control = bus_a.named("playbook.run.completed")[0]

    # the resumed run: dies at segment 2, restarted, completes
    arm2 = {"die": True}
    slow.started.clear()
    runner_c, _ = _runner(db, _Tools(t1=_Tool(t1), slow=_Tool(slow), code_run=_Tool(ScriptedCodeRun(_gated_script(arm2)).handler)))
    run = await runner_c.start_run_background(pb, inputs={})
    await _dead(runner_c, run.id)
    async with db() as s:
        (await s.get(PlaybookRun, run.id)).wake_on_complete = True
        await s.commit()
    arm2["die"] = False
    runner_b, bus_b = _restart(db, _Tools(t1=_Tool(t1), slow=_Tool(slow), code_run=_Tool(ScriptedCodeRun(_gated_script(arm2)).handler)))
    assert await runner_b.resume_interrupted_runs() == 1
    assert (await runner_b.wait_for_run(run.id, timeout=10)).status == "done"
    payload_resumed = bus_b.named("playbook.run.completed")[0]

    assert set(payload_resumed) == set(payload_control)
    assert payload_resumed["wake_on_complete"] is True and payload_control["wake_on_complete"] is True
    assert payload_resumed["playbook_version"] == payload_control["playbook_version"] == 1
    assert payload_resumed["error"] is None and payload_control["error"] is None
    assert payload_resumed["status"] == payload_control["status"] == "done"
    assert payload_resumed["playbook_name"] == payload_control["playbook_name"] == "payload"
    assert payload_resumed["run_id"] == str(run.id)
    assert isinstance(payload_resumed["duration_ms"], int) and payload_resumed["duration_ms"] >= 0


# ------------------------------------------------------------------ Step 5 (real shim)
SEND_A = '''async def run(ctx, inputs):
    try:
        await ctx.tool("send")
    except ctx.OutcomeUnknown:
        return {"unknown": True}
    return "sent"
'''
SEND_B = '''async def run(ctx, inputs):
    await ctx.tool("send")
    return "sent"
'''
SEND_C = '''async def run(ctx, inputs):
    try:
        await ctx.tool("send", _retry=2)
    except ctx.OutcomeUnknown:
        return {"unknown": True}
    return "sent"
'''
FLAKY_RETRY = '''async def run(ctx, inputs):
    try:
        return await ctx.tool("flaky", _retry=2)
    except ctx.OutcomeUnknown:
        return {"unknown": True}
'''


@real_jail
@requires_jail()
@pytest.mark.parametrize("variant", ["a", "b", "c"])
async def test_kill_between_in_flight_and_result_tool_not_re_executed(db, tmp_path, variant):
    src = {"a": SEND_A, "b": SEND_B, "c": SEND_C}[variant]
    send = _Counter("send", die_on=1)  # dies AFTER counting: the row is in flight
    tools = _real_tools(tmp_path, send=send)
    runner_a, _ = _runner(db, tools)
    pb = await _save(db, _pb(f"send-{variant}", src))
    run = await runner_a.start_run_background(pb, inputs={})
    await _dead(runner_a, run.id, timeout=90)
    assert len(send.calls) == 1
    before = await _journal(db, run.id)
    assert len(before) == 2 and before[1]["status"] == "in_flight" and before[1]["attempts"] == []

    runner_b, bus_b = _restart(db, tools)
    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(run.id, timeout=90)
    assert len(send.calls) == 1  # never re-executed — not even with _retry=2
    j = await _journal(db, run.id)
    assert len(j) == len(before)
    assert [e for e in j if e.get("id") == "send" and e["occurrence"] == 1] == [j[1]]
    unknown = j[1]
    message = "outcome unknown — the server restarted while effect send#1 was in flight"
    assert unknown["status"] == "timed_out_unknown"
    assert unknown["error"] == {"type": "OutcomeUnknown", "message": message}
    assert unknown["idempotency_key"] == before[1]["idempotency_key"]
    assert unknown["started_at"] == before[1]["started_at"] and unknown["ended_at"] is not None
    assert len(unknown["attempts"] or []) == 0
    steps = await _steps(db, run.id)
    assert [(s.step_id, s.status) for s in steps] == [("send#1", "failed")]
    assert steps[0].error == f"OutcomeUnknown: {message}"
    done = bus_b.named("playbook.run.completed")
    assert len(done) == 1
    if variant in ("a", "c"):
        assert row.status == "done", (row.error, row.traceback)
        assert runner_b._v2.last_result.value == {"unknown": True}
        assert done[0]["status"] == "done"
        assert row.error is None and row.error_type is None
    else:
        assert row.status == "timed_out_unknown"
        assert row.error_type == "OutcomeUnknown"
        assert "send#1" in row.error and "OutcomeUnknown" in row.error
        assert row.failed_at is not None and row.traceback
        assert done[0]["status"] == "timed_out_unknown"
        assert done[0]["error"] == row.error
    assert await runner_b.resume_interrupted_runs() == 0

    if variant == "c":
        # control: `_retry` is live — a ToolError once, then success → one
        # row, two attempts; only OutcomeUnknown is excluded from retries
        state = {"n": 0}

        async def flaky(**kw):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("flaky once")
            return {"ok": state["n"]}

        tools.add("flaky", flaky)
        pb2 = await _save(db, _pb("flaky-retry", FLAKY_RETRY))
        run2 = await runner_b.start_run_background(pb2, inputs={})
        row2 = await runner_b.wait_for_run(run2.id, timeout=90)
        assert row2.status == "done", (row2.error, row2.traceback)
        assert runner_b._v2.last_result.value == {"ok": 2}
        j2 = await _journal(db, run2.id)
        assert len(j2) == 2 and j2[1]["status"] == "done"
        assert len(j2[1]["attempts"]) == 2
        assert j2[1]["attempts"][0]["error"].startswith("ToolError:") and j2[1]["attempts"][1]["error"] is None
        assert state["n"] == 2


REEXEC_SRC = '''async def run(ctx, inputs):
    a = await ctx.llm("summarize this")
    t = await ctx.now()
    r = await ctx.random()
    await ctx.log("hello")
    after = await ctx.tool("after")
    return {"a": a, "t": t.isoformat(), "r": r, "after": after}
'''


class _DyingAgent(_Agent):
    """`run_llm` dies on its first call (the llm row is in flight), answers after."""

    async def run_llm(self, prompt, **kw):
        self.calls.append((prompt, kw))
        if len(self.calls) == 1:
            raise _ProcessDied()
        return self.result, {"total_tokens": 1}


@real_jail
@requires_jail()
async def test_llm_now_random_re_execute_in_place(db, tmp_path):
    agent = _DyingAgent(result="the summary")
    after = _Counter("after")
    tools = _real_tools(tmp_path, after=after)
    runner_a, _ = _runner(db, tools, agent=agent)
    pb = await _save(db, _pb("reexec", REEXEC_SRC))
    run = await runner_a.start_run_background(pb, inputs={})
    await _dead(runner_a, run.id, timeout=90)
    assert len(agent.calls) == 1
    j = await _journal(db, run.id)
    assert len(j) == 2 and j[1]["kind"] == "llm" and j[1]["status"] == "in_flight"
    # the death also caught `now`/`random`/`log` rows in flight (the host
    # writes them ahead of executing; stale results show the rows are re-run)
    store = DbJournalStore(db)
    rid = str(run.id)
    seq_t = await store.append_in_flight(rid, make_effect_entry(run_id=rid, seq=0, kind="now", id="t", occurrence=1, name=None, args={}))
    seq_r = await store.append_in_flight(rid, make_effect_entry(run_id=rid, seq=0, kind="random", id="r", occurrence=1, name=None, args={}))
    seq_l = await store.append_in_flight(rid, make_effect_entry(run_id=rid, seq=0, kind="log", id="log", occurrence=1, name=None, args={"message": "hello"}))
    assert (seq_t, seq_r, seq_l) == (2, 3, 4)
    async with db() as s:
        for seq, stale in ((2, "1999-01-01T00:00:00+00:00"), (3, 5.0)):
            r = await s.get(PlaybookJournal, (run.id, seq))
            r.result = stale
        await s.commit()
    before = await _journal(db, run.id)
    keys_before = [e["idempotency_key"] for e in before[1:]]

    runner_b, bus_b = _restart(db, tools, agent=agent)
    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(run.id, timeout=90)
    assert row.status == "done", (row.error, row.traceback)
    assert len(agent.calls) == 2 and agent.calls[1][0] == "summarize this"
    after_j = await _journal(db, run.id)
    assert [e["seq"] for e in after_j] == [0, 1, 2, 3, 4, 5]
    assert [e["kind"] for e in after_j[1:]] == ["llm", "now", "random", "log", "tool"]
    assert [e["status"] for e in after_j[1:]] == ["done"] * 5
    assert [e["idempotency_key"] for e in after_j[1:5]] == keys_before  # same seq, same key
    assert after_j[1]["result"] == "the summary" and len(after_j[1]["attempts"]) == 1
    assert after_j[2]["result"] != "1999-01-01T00:00:00+00:00" and after_j[2]["result"].startswith("20")
    assert isinstance(after_j[3]["result"], float) and 0 <= after_j[3]["result"] < 1 and after_j[3]["result"] != 5.0
    assert after_j[4]["result"] == {"message": "hello"} and after_j[4]["args"] == {"message": "hello"}
    assert [e["started_at"] for e in after_j[1:5]] == [e["started_at"] for e in before[1:5]]
    assert len(after.calls) == 1
    value = runner_b._v2.last_result.value
    assert value["a"] == "the summary" and value["t"] == after_j[2]["result"] and value["r"] == after_j[3]["result"]
    steps = await _steps(db, run.id)
    assert [(s.step_id, s.status) for s in steps] == [
        ("a#1", "done"), ("t#1", "done"), ("r#1", "done"), ("log#1", "done"), ("after#1", "done"),
    ]
    assert bus_b.named("playbook.run.completed")[0]["status"] == "done"


GATHER_SRC = '''async def run(ctx, inputs):
    try:
        a, b, c = await ctx.gather(ctx.tool("t1"), ctx.tool("t2"), ctx.tool("t3"))
    except ctx.OutcomeUnknown as e:
        x, y, z = await ctx.gather(ctx.tool("t1", again=True), ctx.tool("t2", again=True), ctx.tool("t3", again=True))
        return {"first": str(e), "second": [x, y, z]}
    return {"first": [a, b, c]}
'''


@real_jail
@requires_jail()
async def test_gather_across_restart_order_and_first_unknown(db, tmp_path):
    gate = asyncio.Event()
    t1, t2, t3 = _Counter("t1"), _Counter("t2", gate=gate), _Counter("t3")
    tools = _real_tools(tmp_path, t1=t1, t2=t2, t3=t3)
    runner_a, bus_a = _runner(db, tools)
    pb = await _save(db, _pb("gather-restart", GATHER_SRC))
    run = await runner_a.start_run_background(pb, inputs={})
    await asyncio.wait_for(t2.started.wait(), 90)
    # t1/t3 complete (rows done, step events out) before the death hits
    # inside t2 — observed on the bus, not by polling the DB under the run
    # (the in-memory sqlite engine shares one connection; a concurrent
    # reader session's close would interleave with the writer's transaction)
    await _until(lambda: {p["step_id"] for p in bus_a.named("playbook.step.completed")} == {"t1#1", "t3#1"}, 90)
    t2.die_after_gate = True
    gate.set()
    await _dead(runner_a, run.id, timeout=90)
    before = await _journal(db, run.id)
    assert [(e["seq"], e["name"], e["status"]) for e in before[1:]] == [(1, "t1", "done"), (2, "t2", "in_flight"), (3, "t3", "done")]

    gate.clear()
    t2.die_after_gate = False
    t2.gate = None
    runner_b, bus_b = _restart(db, tools)
    assert await runner_b.resume_interrupted_runs() == 1
    row = await runner_b.wait_for_run(run.id, timeout=90)
    assert row.status == "done", (row.error, row.traceback)
    j = await _journal(db, run.id)
    assert [(e["seq"], e["name"], e["status"]) for e in j[1:]] == [
        (1, "t1", "done"), (2, "t2", "timed_out_unknown"), (3, "t3", "done"),
        (4, "t1", "done"), (5, "t2", "done"), (6, "t3", "done"),
    ]
    assert j[1] == before[1] and j[3] == before[3]  # t1/t3 rows untouched by the resume
    assert j[2]["error"]["type"] == "OutcomeUnknown" and "t2#1" in j[2]["error"]["message"]
    # the first gather's t1/t3 were NOT re-executed: one call each without `again`
    assert [c for c in t1.calls if not c.get("again")] == [{}] and [c for c in t3.calls if not c.get("again")] == [{}]
    assert [c for c in t2.calls if not c.get("again")] == [{}]  # the lost one
    assert [len(t.calls) for t in (t1, t2, t3)] == [2, 2, 2]
    value = runner_b._v2.last_result.value
    assert "t2#1" in value["first"] and "unknown" in value["first"]
    assert [v["t"] for v in value["second"]] == ["t1", "t2", "t3"]  # argument order
    assert all(v["again"] is True for v in value["second"])
    assert [e["args"] for e in j[4:]] == [{"again": True}] * 3


SUB_A = '''async def run(ctx, inputs):
    try:
        return await ctx.subtask("sub-b", {})
    except ctx.OutcomeUnknown as e:
        return {"a": "unknown", "detail": str(e)}
'''
SUB_B = '''async def run(ctx, inputs):
    try:
        return await ctx.tool("bt")
    except ctx.OutcomeUnknown:
        try:
            await ctx.subtask("sub-a", {})
        except ctx.EffectError as e:
            return {"b": str(e)}
        return {"b": "no guard"}
'''


@real_jail
@requires_jail()
async def test_subtask_parent_child_rows_and_cycle_guard_across_restart(db_file, tmp_path):
    db = db_file  # A and B resume concurrently: per-session connections (see `db_file`)
    bt = _Counter("bt", die_on=1)
    tools = _real_tools(tmp_path, bt=bt)
    runner_a, _ = _runner(db, tools)
    pb_a = await _save(db, _pb("sub-a", SUB_A))
    await _save(db, _pb("sub-b", SUB_B))
    run_a = await runner_a.start_run_background(pb_a, inputs={})
    await _dead(runner_a, run_a.id, timeout=90)
    async with db() as s:
        rows = list((await s.execute(select(PlaybookRun).order_by(PlaybookRun.started_at))).scalars().all())
    assert len(rows) == 2
    run_b = next(r for r in rows if r.id != run_a.id)
    assert run_b.status == "running" and run_b.parent_run_id == run_a.id
    ja, jb = await _journal(db, run_a.id), await _journal(db, run_b.id)
    assert ja[1]["kind"] == "subtask" and ja[1]["status"] == "in_flight"
    assert jb[1]["kind"] == "tool" and jb[1]["name"] == "bt" and jb[1]["status"] == "in_flight"
    assert len(bt.calls) == 1

    runner_b, bus_b = _restart(db, tools)
    assert await runner_b.resume_interrupted_runs() == 2
    row_a = await runner_b.wait_for_run(run_a.id, timeout=90)
    row_b = await runner_b.wait_for_run(run_b.id, timeout=90)
    assert row_a.status == "done", (row_a.error, row_a.traceback)
    assert row_b.status == "done", (row_b.error, row_b.traceback)
    assert len(bt.calls) == 1
    ja, jb = await _journal(db, run_a.id), await _journal(db, run_b.id)
    assert ja[1]["status"] == "timed_out_unknown" and "sub-b#1" in ja[1]["error"]["message"]
    assert len(ja) == 2
    assert jb[1]["status"] == "timed_out_unknown" and "bt#1" in jb[1]["error"]["message"]
    # B's `ctx.subtask("sub-a")` tripped the cycle guard on the chain rebuilt
    # from parent_run_id (A -> B -> A); no third run row
    assert jb[2]["kind"] == "subtask" and jb[2]["name"] == "sub-a" and jb[2]["status"] == "failed_handled"
    assert jb[2]["error"]["type"] == "EffectError" and "would recurse" in jb[2]["error"]["message"]
    assert "sub-a -> sub-b -> sub-a" in jb[2]["error"]["message"]
    async with db() as s:
        n = (await s.execute(select(func.count()).select_from(PlaybookRun))).scalar_one()
    assert n == 2
    row_b = await _row(db, run_b.id)
    assert row_b.parent_run_id == run_a.id and row_b.trigger == f"subtask:{run_a.id}"
    done = {p["run_id"]: p for p in bus_b.named("playbook.run.completed")}
    assert set(done) == {str(run_a.id), str(run_b.id)}
    assert done[str(run_a.id)]["parent_run_id"] is None
    assert done[str(run_b.id)]["parent_run_id"] == str(run_a.id)
    assert done[str(run_b.id)]["trigger"] == f"subtask:{run_a.id}"
    assert all(p["status"] == "done" for p in done.values())
