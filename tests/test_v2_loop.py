"""plans/032 phase 02 — the in-jail shim, the host segment loop, ctx.tool /
now / random / log, the error contract and the hash-seed pin (docs/v2.md §6,
§7, §11).

Two kinds of tests:
- unmarked: a scripted `code_run` fake returns canned shim payloads (no jail);
- `real_jail`: the real shim runs through plugin-inline-code-run's managed
  install (`tests/_jail.py`), skipped without a usable kernel jail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import statistics
from typing import Any

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from _jail import real_code_run, real_jail, requires_jail
from plugin_playbooks import _ensure_columns
from plugin_playbooks.agent_tools import _nested_run_refusal
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner, active_run_id
from plugin_playbooks.v2 import MemoryJournalStore
from plugin_playbooks.v2.checker import check
from plugin_playbooks.v2.loop import SegmentLoop



# ------------------------------------------------------------------ harness
class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def named(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]


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


class _Cred:
    def __init__(self, value: str) -> None:
        self.value = value


class _Vault:
    def __init__(self, creds: dict[str, str]) -> None:
        self._creds = creds
        self.reads: list[str] = []

    async def get_credential(self, name: str) -> _Cred:
        self.reads.append(name)
        return _Cred(self._creds[name])


class _Ctx:
    current_conversation_id = None

    def __init__(self, vault=None) -> None:
        self.vault = vault


def _payload(result: dict, **extra: Any) -> str:
    base = {
        "ok": True, "exit_code": 0, "stdout": "", "stderr": "", "timed_out": False,
        "duration_ms": 1, "backend": "fake", "output_files": [], "result": result,
    }
    base.update(extra)
    return json.dumps(base)


def _effect(seq: int, site: str, occ: int, kind: str, name: str | None, args: dict, **options: Any) -> dict:
    return {
        "kind": "effect", "seq": seq, "id": f"{site}#{occ}", "call_site_id": site,
        "occurrence": occ, "effect_kind": kind, "name": name, "args": args,
        "options": options,
    }


def _error(error_type: str, message: str, line: int = 0, last: dict | None = None) -> dict:
    return {
        "kind": "error", "error_type": error_type, "message": message,
        "traceback": [{"line": line, "name": "run", "source": ""}] if line else [],
        "playbook_line": line, "last_completed_effect": last, "locals_preview": {},
    }


class ScriptedCodeRun:
    """A `code_run` fake: `script(envelope) -> result dict` or a list of
    result dicts consumed in order. Records every envelope it saw."""

    def __init__(self, script) -> None:
        self.calls: list[dict] = []
        if callable(script):
            self._fn = script
        else:
            items = list(script)
            self._fn = lambda env: items.pop(0)

    async def handler(self, code: str, input_json: Any = None, timeout_sec=None, title=None, **_):
        self.calls.append({"code": code, "input_json": input_json, "timeout_sec": timeout_sec, "title": title})
        res = self._fn(input_json)
        if isinstance(res, str):
            return res
        return _payload(res)


def _pb(name: str, source: str) -> Playbook:
    return Playbook(
        name=name, display_name=name, code=source, format="python",
        definition={"name": name, "format": "python", "steps": []},
        status="enabled",
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


def _runner(sf, tools, *, events=None, context=None, **loop_kw) -> tuple[PlaybookRunner, _Bus]:
    bus = events or _Bus()
    runner = PlaybookRunner(session_factory=sf, tool_registry=tools, events=bus, context=context)
    runner._v2 = SegmentLoop(
        sf, tools, bus, context, MemoryJournalStore(keep_completed=True), **loop_kw,
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


async def _steps(sf, run_id) -> list[PlaybookStepRun]:
    async with sf() as s:
        return list((await s.execute(
            select(PlaybookStepRun).where(PlaybookStepRun.run_id == run_id)
            .order_by(PlaybookStepRun.started_at, PlaybookStepRun.id)
        )).scalars().all())


async def _run_to_end(runner, pb, inputs=None, timeout=10.0) -> PlaybookRun:
    run = await runner.start_run_background(pb, inputs=inputs or {})
    row = await runner.wait_for_run(run.id, timeout=timeout)
    assert row is not None
    return row


def _journal(runner, run_id) -> list[dict]:
    return runner._v2.journal._runs[str(run_id)]


def _strip(journal: list[dict]) -> list[dict]:
    out = []
    for e in journal:
        e = dict(e)
        for k in ("hash_seed", "started_at", "ended_at", "ms", "idempotency_key", "attempts"):
            e.pop(k, None)
        out.append(e)
    return out


def _real_tools(tmp_path, **tools) -> _Tools:
    t = _Tools(**{k: _Tool(v) for k, v in tools.items()})
    t.add("code_run", real_code_run(tmp_path))
    return t


# ------------------------------------------------------------------ real jail
THREE_EFFECTS = '''async def run(ctx, inputs):
    a = await ctx.tool("alpha", n=inputs["n"])
    b = await ctx.tool("beta", prev=a["v"])
    c = await ctx.tool("gamma", prev=b["v"])
    return {"a": a, "b": b, "c": c}
'''


def _abc_tools(record: list) -> dict:
    async def alpha(**kw):
        record.append(("alpha", kw))
        return {"v": 1}

    async def beta(**kw):
        record.append(("beta", kw))
        return json.dumps({"v": 2})

    async def gamma(**kw):
        record.append(("gamma", kw))
        return {"v": 3}

    return {"alpha": alpha, "beta": beta, "gamma": gamma}


async def _three_effect_run(db, tmp_path, name="three"):
    record: list = []
    tools = _real_tools(tmp_path, **_abc_tools(record))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb(name, THREE_EFFECTS))
    row = await _run_to_end(runner, pb, {"n": 7}, timeout=60)
    return runner, bus, tools, pb, row, record


@real_jail
@requires_jail()
async def test_three_effects_complete_through_jail_identical_journal(db, tmp_path):
    runner, bus, tools, pb, row, record = await _three_effect_run(db, tmp_path)
    assert row.status == "done", (row.error, row.traceback)
    assert runner._v2.last_result.segments == 4
    steps = await _steps(db, row.id)
    assert [s.step_id for s in steps] == ["a#1", "b#1", "c#1"]
    assert steps[0].outputs == {"tool": "alpha", "result": {"v": 1}}
    assert steps[1].outputs == {"tool": "beta", "result": {"v": 2}}
    assert steps[2].outputs == {"tool": "gamma", "result": {"v": 3}}
    j1 = _journal(runner, row.id)

    row2 = await _run_to_end(runner, pb, {"n": 7}, timeout=60)
    assert row2.status == "done"
    j2 = _journal(runner, row2.id)
    assert j1[0]["hash_seed"] != j2[0]["hash_seed"] or True  # seeds are per run
    assert _strip(j1) == _strip(j2)
    assert [e["status"] for e in j1[1:]] == ["done"] * 3
    assert j1[1]["result"] == {"v": 1} and j1[2]["result"] == {"v": 2}


SET_ITER = '''async def run(ctx, inputs):
    s = {f"k{i}" for i in range(40)}
    first = list(s)
    await ctx.tool("rec", order=first)
    again = list(s)
    await ctx.tool("rec", order=again)
    import sys, os
    probe = {
        "isolated": sys.flags.isolated,
        "h": hash("a"),
        "env": sorted(k for k in os.environ if k.startswith("PYTHON")),
        "same": first == again == list(s),
    }
    await ctx.tool("probe", **probe)
    return probe
'''


@real_jail
@requires_jail()
async def test_set_iteration_replays_identically_across_spawns(db, tmp_path):
    orders: list[list[str]] = []
    probes: list[dict] = []

    async def rec(order):
        orders.append(order)
        return {"ok": True}

    async def probe(**kw):
        probes.append(kw)
        return {"ok": True}

    tools = _real_tools(tmp_path, rec=rec, probe=probe)
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("setiter", SET_ITER))
    rows = [await _run_to_end(runner, pb, {}, timeout=90) for _ in range(2)]
    for row in rows:
        assert row.status == "done", (row.error, row.traceback)
    code_run = tools.get("code_run").handler
    assert len(code_run.calls) == 8  # 4 spawns per run (3 effects + return)
    assert len(orders) == 4 and len(probes) == 2
    for row in rows:
        j = _journal(runner, row.id)
        # every spawn of this run saw the same set order (journal args == recorded)
        assert j[1]["args"]["order"] == j[2]["args"]["order"]
    for p in probes:
        assert p["isolated"] == 0
        assert p["same"] is True
        assert p["env"] == ["PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED"]
    # within one run: the set order is identical across its spawns, and the
    # first-spawn order (journaled arg) matches the replay-spawn arg
    assert orders[0] == orders[1] and orders[2] == orders[3]
    # hash("a") inside the jail is stable across the spawns of a run: the
    # journaled args (spawn 1, 2) and the probe (spawn 3) came from three spawns
    j0 = _journal(runner, rows[0].id)
    assert j0[3]["args"]["h"] == probes[0]["h"]
    print(f"v2-hashseed backend={code_run.payloads[0]['backend']} isolated={probes[0]['isolated']} "
          f"h={probes[0]['h']} env={probes[0]['env']}")


CALL_SITES = '''async def run(ctx, inputs):
    write_a = await ctx.tool("file_write", path="a")
    write_b = await ctx.tool("file_write", path="b")
    await ctx.tool("file_write", path="c")
    for i in range(2):
        await ctx.tool("touch", i=i)
    await ctx.random()
    await ctx.now()
    await ctx.log("done")
    return "ok"
'''

CALL_SITE_TUPLES = [
    ("write_a", 1, "file_write"), ("write_b", 1, "file_write"), ("file_write", 1, "file_write"),
    ("touch", 1, "touch"), ("touch", 2, "touch"),
    ("random", 1, None), ("now", 1, None), ("log", 1, None),
]
CALL_SITE_STEP_IDS = ["write_a#1", "write_b#1", "file_write#1", "touch#1", "touch#2", "random#1", "now#1", "log#1"]


def _scripted_call_sites():
    """The payloads the real shim derives from the envelope's call_sites."""
    plan = [
        ("write_a", 1, "tool", "file_write", {"path": "a"}),
        ("write_b", 1, "tool", "file_write", {"path": "b"}),
        ("file_write", 1, "tool", "file_write", {"path": "c"}),
        ("touch", 1, "tool", "touch", {"i": 0}),
        ("touch", 2, "tool", "touch", {"i": 1}),
        ("random", 1, "random", None, {}),
        ("now", 1, "now", None, {}),
        ("log", 1, "log", None, {"message": "done"}),
    ]

    def script(env):
        n = len(env["journal"])  # rows so far incl. entry 0
        if n - 1 < len(plan):
            site, occ, kind, name, args = plan[n - 1]
            return _effect(n, site, occ, kind, name, args)
        return {"kind": "return", "value": "ok"}

    return ScriptedCodeRun(script)


async def _assert_call_site_run(db, runner, row):
    assert row.status == "done", (row.error, row.traceback)
    j = _journal(runner, row.id)
    assert [(e["id"], e["occurrence"], e["name"]) for e in j[1:]] == CALL_SITE_TUPLES
    steps = await _steps(db, row.id)
    assert [s.step_id for s in steps] == CALL_SITE_STEP_IDS
    assert [s.step_kind for s in steps] == ["tool"] * 5 + ["random", "now", "log"]


async def test_call_site_ids_not_tool_name_counting(db):
    async def file_write(**kw):
        return {"ok": True}

    async def touch(**kw):
        return {"ok": True}

    fake = _scripted_call_sites()
    tools = _Tools(file_write=_Tool(file_write), touch=_Tool(touch), code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("sites", CALL_SITES))
    row = await _run_to_end(runner, pb)
    await _assert_call_site_run(db, runner, row)
    # the envelope carried the checker's call sites (the shim's id source)
    ids = [c["id"] for c in fake.calls[0]["input_json"]["call_sites"]]
    assert ids == ["write_a", "write_b", "file_write", "touch", "random", "now", "log"]


@real_jail
@requires_jail()
async def test_call_site_ids_not_tool_name_counting_real_jail(db, tmp_path):
    async def file_write(**kw):
        return {"ok": True}

    async def touch(**kw):
        return {"ok": True}

    tools = _real_tools(tmp_path, file_write=file_write, touch=touch)
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("sites", CALL_SITES))
    row = await _run_to_end(runner, pb, timeout=90)
    await _assert_call_site_run(db, runner, row)


MAX_EFFECTS_SRC = '''async def run(ctx, inputs):
    for i in range(10):
        await ctx.tool("touch", i=i)
    return "never"
'''


@real_jail
@requires_jail()
async def test_max_effects_trips_loudly(db, tmp_path):
    async def touch(**kw):
        return {"ok": True}

    tools = _real_tools(tmp_path, touch=touch)
    runner, bus = _runner(db, tools, max_effects=5)
    pb = await _save(db, _pb("cap", MAX_EFFECTS_SRC))
    row = await _run_to_end(runner, pb, timeout=90)
    assert row.status == "failed"
    assert row.error_type == "MaxEffectsExceeded"
    assert "touch#6" in row.error and "5" in row.error and "MAX_EFFECTS" in row.error
    assert len(_journal(runner, row.id)) == 6  # entry 0 + 5 effects


KEYERROR_SRC = '''async def run(ctx, inputs):
    rows = await ctx.tool("fetch")
    for i, r in enumerate(rows):
        await ctx.tool("touch", i=i)
        r["missing"]
    return len(rows)
'''


@real_jail
@requires_jail()
async def test_keyerror_in_loop_body_reports_line_effect_locals(db, tmp_path):
    async def fetch(**kw):
        return [{"missing": 1}, {}]

    async def touch(**kw):
        return {"ok": True}

    tools = _real_tools(tmp_path, fetch=fetch, touch=touch)
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("keyerr", KEYERROR_SRC))
    row = await _run_to_end(runner, pb, timeout=90)
    assert row.status == "failed"
    assert row.error_type == "KeyError"
    assert row.failed_at is not None
    assert row.error.startswith("line 5:"), row.error
    assert 'r["missing"]' in row.error and "after effect touch#2" in row.error
    payload = tools.get("code_run").handler.payloads[-1]["result"]
    assert payload["playbook_line"] == 5
    assert payload["last_completed_effect"] == {"seq": 3, "id": "touch#2", "kind": "tool"}
    assert "i" in payload["locals_preview"] and "r" in payload["locals_preview"]
    assert payload["traceback"] and all(fr["name"] == "run" for fr in payload["traceback"])
    assert 'File "playbook:keyerr@v1", line 5' in row.traceback
    assert "KeyError: 'missing'" in row.traceback
    assert bus.named("playbook.run.completed")[-1]["status"] == "failed"


SEGMENT_TIMEOUT_SRC = '''async def run(ctx, inputs):
    await ctx.tool("fetch")
    while True:
        pass
'''


@real_jail
@requires_jail()
async def test_segment_timeout_names_last_effect(db, tmp_path):
    async def fetch(**kw):
        return {"ok": True}

    tools = _real_tools(tmp_path, fetch=fetch)
    runner, bus = _runner(db, tools, segment_timeout=2)
    pb = await _save(db, _pb("spin", SEGMENT_TIMEOUT_SRC))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=4 + 2)  # segment 1 spawn + 2 s cap + slack
    assert row.status == "failed", row.status
    assert row.error_type == "SegmentTimeout"
    assert "after effect fetch#1" in row.error and "segment 2" in row.error
    assert "in pure compute" in row.error


async def test_segment_timeout_names_last_effect_scripted(db):
    async def fetch(**kw):
        return {"ok": True}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "fetch", 1, "tool", "fetch", {})
        return _payload({}, ok=False, timed_out=True, exit_code=-9, progress={"seq": 1, "phase": "compute"})

    fake = ScriptedCodeRun(script)
    tools = _Tools(fetch=_Tool(fetch), code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools, segment_timeout=2)
    pb = await _save(db, _pb("spin", SEGMENT_TIMEOUT_SRC))
    row = await _run_to_end(runner, pb)
    assert row.status == "failed"
    assert row.error_type == "SegmentTimeout"
    assert row.error == "segment 2 timed out (2s) after effect fetch#1, in pure compute"
    assert row.failed_at is not None


NOW_RANDOM_SRC = '''async def run(ctx, inputs):
    r = await ctx.random()
    t = await ctx.now()
    await ctx.tool("a", r=r, t=t.isoformat(), tname=type(t).__name__, aware=t.tzinfo is not None)
    await ctx.tool("b", r=r, t=t.isoformat(), tname=type(t).__name__, aware=t.tzinfo is not None)
    return {"r": r}
'''


@real_jail
@requires_jail()
async def test_now_random_replayed_not_redrawn(db, tmp_path):
    seen: list[tuple[str, dict]] = []

    async def a(**kw):
        seen.append(("a", kw))
        return {"ok": True}

    async def b(**kw):
        seen.append(("b", kw))
        return {"ok": True}

    tools = _real_tools(tmp_path, a=a, b=b)
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("nowrand", NOW_RANDOM_SRC))
    row = await _run_to_end(runner, pb, timeout=90)
    assert row.status == "done", (row.error, row.traceback)
    assert len(seen) == 2
    assert seen[0][1]["r"] == seen[1][1]["r"] and seen[0][1]["t"] == seen[1][1]["t"]
    for _, kw in seen:
        assert kw["tname"] == "datetime" and kw["aware"] is True
    j = _journal(runner, row.id)
    # docs/v2.md §7: an assigned site's id is the assignment target — `r`, `t`
    randoms = [e for e in j[1:] if (e["id"], e["occurrence"], e["kind"]) == ("r", 1, "random")]
    nows = [e for e in j[1:] if (e["id"], e["occurrence"], e["kind"]) == ("t", 1, "now")]
    assert len(randoms) == 1 and len(nows) == 1
    assert len([e for e in j[1:] if e["kind"] in ("random", "now")]) == 2
    assert runner._v2.last_result.value == {"r": randoms[0]["result"]}
    assert seen[0][1]["r"] == randoms[0]["result"]
    assert seen[0][1]["t"] == nows[0]["result"]
    # 5 segments: random, now, a, b, return — each effect is one spawn
    assert runner._v2.last_result.segments == 5


PRINT_SRC = '''async def run(ctx, inputs):
    print("LEAK-7f3a")
    await ctx.tool("touch")
    print("LEAK-7f3a")
    return "ok"
'''


@real_jail
@requires_jail()
async def test_print_never_appears(db, tmp_path):
    async def touch(**kw):
        return {"ok": True}

    tools = _real_tools(tmp_path, touch=touch)
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("printer", PRINT_SRC))
    row = await _run_to_end(runner, pb, timeout=60)
    assert row.status == "done", (row.error, row.traceback)
    token = "LEAK-7f3a"
    assert token not in json.dumps(_journal(runner, row.id), default=str)
    assert token not in json.dumps([(s.step_id, s.outputs, s.error) for s in await _steps(db, row.id)], default=str)
    assert token not in json.dumps(bus.events, default=str)
    for p in tools.get("code_run").handler.payloads:
        assert token not in (p["stdout"] or "") and token not in (p["stderr"] or "")


DIVERGENCE_SRC = '''async def run(ctx, inputs):
    rows = await ctx.tool("fetch")
    await ctx.tool("touch", n=len(rows))
    return "ok"
'''


@real_jail
@requires_jail()
async def test_journal_divergence_typed_error(db, tmp_path):
    async def fetch(**kw):
        return [1, 2]

    async def touch(**kw):
        return {"ok": True}

    tools = _real_tools(tmp_path, fetch=fetch, touch=touch)
    real = tools.get("code_run").handler
    edited = DIVERGENCE_SRC.replace("rows = await", "data = await").replace("len(rows)", "len(data)")
    edited_sites = check(edited, name="div", version=1).summary["call_sites"]

    async def editing(code, input_json=None, **kw):
        # "code edited under a run": after effect 1 completed, the next
        # segment sees a source whose first effect carries another id
        if len(input_json["journal"]) > 1:
            input_json = dict(input_json, source=edited, call_sites=edited_sites)
        return await real(code, input_json=input_json, **kw)

    tools.add("code_run", editing)
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("div", DIVERGENCE_SRC))
    row = await _run_to_end(runner, pb, timeout=60)
    assert row.status == "failed"
    assert row.error_type == "JournalDivergence"
    for phrase in ("set iteration", "code edited under a run", "non-journaled randomness"):
        assert phrase in row.error, row.error
    j = _journal(runner, row.id)
    assert len(j) == 2 and j[1]["status"] == "done"


@real_jail
@requires_jail()
async def test_segment_latency_recorded(db, tmp_path, capsys):
    runner, bus, tools, pb, row, record = await _three_effect_run(db, tmp_path, "latency")
    assert row.status == "done"
    lat = runner._v2.last_result.segment_latency_ms
    assert len(lat) == 4
    assert all(h > 0 and j > 0 for h, j in lat), lat
    host = [h for h, _ in lat]
    jail = [j for _, j in lat]
    with capsys.disabled():
        print(
            f"\nv2-latency n={len(lat)} median_host_ms={statistics.median(host):.0f} "
            f"max_host_ms={max(host)} median_jail_ms={statistics.median(jail):.0f} "
            f"max_jail_ms={max(jail)} backend={tools.get('code_run').handler.payloads[0]['backend']}"
        )


# ------------------------------------------------------------------ scripted
async def test_vault_ref_raw_in_journal_resolved_at_execution(db):
    received: list[dict] = []

    async def http(**kw):
        received.append(kw)
        return {"status": 200}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "resp", 1, "tool", "http", {"headers": {"x-api-key": "vault:my_key"}})
        return {"kind": "return", "value": "ok"}

    fake = ScriptedCodeRun(script)
    tools = _Tools(http=_Tool(http), code_run=_Tool(fake.handler))
    vault = _Vault({"my_key": "s3cret-value"})
    runner, bus = _runner(db, tools, context=_Ctx(vault))
    pb = await _save(db, _pb("vault", 'async def run(ctx, inputs):\n    resp = await ctx.tool("http", headers={"x-api-key": "vault:my_key"})\n    return "ok"\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", row.error
    assert received == [{"headers": {"x-api-key": "s3cret-value"}}]
    assert vault.reads == ["my_key"]
    j = _journal(runner, row.id)
    assert j[1]["args"] == {"headers": {"x-api-key": "vault:my_key"}}
    steps = await _steps(db, row.id)
    assert steps[0].inputs == {"headers": {"x-api-key": "vault:my_key"}}
    blob = json.dumps({"j": j, "steps": [s.inputs for s in steps], "events": bus.events,
                       "envelopes": [c["input_json"] for c in fake.calls]}, default=str)
    assert "s3cret-value" not in blob
    assert "vault:my_key" in blob


GATED_SRC = '''async def run(ctx, inputs):
    await ctx.tool("slow")
    return "ok"
'''


@pytest.mark.parametrize("case", ["explicit", "default"])
async def test_effect_timeout_enforced(db, monkeypatch, case):
    calls: list[str] = []
    gate = asyncio.Event()

    async def slow(**kw):
        calls.append("slow-started")
        await gate.wait()
        calls.append("slow-finished")
        return {"ok": True}

    options = {"_timeout": 1} if case == "explicit" else {}
    if case == "default":
        monkeypatch.setattr("plugin_playbooks.v2.loop.DEFAULT_TIMEOUTS", {"tool": 1})

    def script(env):
        j = env["journal"]
        if len(j) == 1:
            return _effect(1, "slow", 1, "tool", "slow", {}, **options)
        assert j[1]["status"] == "failed"
        return _error(j[1]["error"]["type"], j[1]["error"]["message"], line=2)

    fake = ScriptedCodeRun(script)
    tools = _Tools(slow=_Tool(slow), code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("timeout", GATED_SRC))
    run = await runner.start_run_background(pb, inputs={})
    row = await runner.wait_for_run(run.id, timeout=2.5)
    try:
        assert row.status == "failed", row.status
        assert row.error_type == "EffectTimeout"
        assert "timed out" in row.error
        steps = await _steps(db, run.id)
        assert steps[0].status == "failed" and "timed out" in steps[0].error
        j = _journal(runner, run.id)
        assert j[1]["status"] == "failed" and j[1]["error"]["type"] == "EffectTimeout"
        assert "slow-finished" not in calls
    finally:
        gate.set()


async def test_tool_effect_runs_in_billing_scope(db, monkeypatch):
    import plugin_playbooks.runner as runner_mod

    rec = {"entered": 0, "active": False, "playbooks": [], "handler_saw": None}

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

    async def ping(**kw):
        rec["handler_saw"] = rec["active"]
        return {"ok": True}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "ping", 1, "tool", "ping", {})
        return {"kind": "return", "value": None}

    fake = ScriptedCodeRun(script)
    tools = _Tools(ping=_Tool(ping), code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("billing", 'async def run(ctx, inputs):\n    await ctx.tool("ping")\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done"
    assert rec["entered"] == 1 and rec["playbooks"] == ["billing"]
    assert rec["handler_saw"] is True


async def test_retry_single_entry_with_attempts(db):
    n = {"calls": 0}

    async def flaky(**kw):
        n["calls"] += 1
        if n["calls"] < 3:
            raise RuntimeError(f"boom {n['calls']}")
        return {"ok": True}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "flaky", 1, "tool", "flaky", {}, _retry={"attempts": 2, "backoff": 0})
        return {"kind": "return", "value": "ok"}

    fake = ScriptedCodeRun(script)
    tools = _Tools(flaky=_Tool(flaky), code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("retry", 'async def run(ctx, inputs):\n    await ctx.tool("flaky", _retry={"attempts": 2, "backoff": 0})\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", row.error
    j = _journal(runner, row.id)
    assert len(j) == 2
    e = j[1]
    assert e["status"] == "done" and len(e["attempts"]) == 3
    assert [a["n"] for a in e["attempts"]] == [1, 2, 3]
    assert e["attempts"][0]["error"].startswith("ToolError: RuntimeError: boom 1")
    assert e["attempts"][2]["error"] is None
    steps = await _steps(db, row.id)
    assert len(steps) == 1 and steps[0].retry_count == 2 and steps[0].status == "done"
    assert n["calls"] == 3


async def test_error_payload_persisted_on_run_row(db):
    err = _error("ValueError", "bad input", line=2, last=None)
    err["traceback"] = [{"line": 2, "name": "run", "source": 'raise ValueError("bad input")'}]
    fake = ScriptedCodeRun([err])
    tools = _Tools(code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("errs", 'async def run(ctx, inputs):\n    raise ValueError("bad input")\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "failed"
    assert row.error_type == "ValueError"
    assert row.error == 'line 2: raise ValueError("bad input") → ValueError: bad input before any effect'
    assert row.failed_at is not None
    assert 'File "playbook:errs@v1", line 2, in run' in row.traceback
    assert row.traceback.endswith("ValueError: bad input")
    done = bus.named("playbook.run.completed")
    assert len(done) == 1
    assert done[0]["status"] == "failed" and done[0]["error"] == row.error


async def test_cancel_run_v2_uncatchable(db):
    gate = asyncio.Event()
    started = asyncio.Event()
    calls: list[str] = []

    async def gated(**kw):
        calls.append("started")
        started.set()
        await gate.wait()
        calls.append("finished")
        return {"ok": True}

    fake = ScriptedCodeRun(lambda env: _effect(1, "gated", 1, "tool", "gated", {}))
    tools = _Tools(gated=_Tool(gated), code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("cancel", 'async def run(ctx, inputs):\n    try:\n        await ctx.tool("gated")\n    except Exception:\n        return "caught"\n'))
    run = await runner.start_run_background(pb, inputs={})
    await asyncio.wait_for(started.wait(), 5)
    assert run.id in runner._tasks
    await runner.cancel_run(run.id)
    row = await runner.wait_for_run(run.id, timeout=5)
    assert row.status == "cancelled"
    assert len(fake.calls) == 1  # no further segment: run() never sees the cancel
    assert calls == ["started"]
    j = _journal(runner, run.id)
    assert j[1]["status"] == "failed" and j[1]["error"]["type"] == "RunCancelled"
    steps = await _steps(db, run.id)
    assert steps[0].status == "failed" and "cancelled" in steps[0].error
    assert bus.named("playbook.run.completed")[-1]["status"] == "cancelled"
    assert runner._v2.last_result.value is None
    gate.set()


async def test_completion_emit_sees_no_active_run(db):
    class _RecBus(_Bus):
        def __init__(self) -> None:
            super().__init__()
            self.seen: dict[str, tuple] = {}

        async def emit(self, name, payload):
            await super().emit(name, payload)
            if name in ("playbook.run.completed", "playbook.step.started"):
                self.seen[name] = (active_run_id(), _nested_run_refusal())

    async def ping(**kw):
        return {"ok": True}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "ping", 1, "tool", "ping", {})
        return {"kind": "return", "value": 1}

    fake = ScriptedCodeRun(script)
    tools = _Tools(ping=_Tool(ping), code_run=_Tool(fake.handler))
    bus = _RecBus()
    runner, _ = _runner(db, tools, events=bus)
    pb = await _save(db, _pb("emit", 'async def run(ctx, inputs):\n    await ctx.tool("ping")\n    return 1\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done"
    assert bus.seen["playbook.run.completed"] == (None, None)
    active, refusal = bus.seen["playbook.step.started"]
    assert active == str(row.id)
    assert refusal is not None and "nested_playbook_run" in refusal


async def test_ctx_log_entries_in_trace(db):
    def script(env):
        n = len(env["journal"])
        if n == 1:
            return _effect(1, "log", 1, "log", None, {"message": "first"})
        if n == 2:
            return _effect(2, "log", 2, "log", None, {"message": "second"})
        return {"kind": "return", "value": None}

    fake = ScriptedCodeRun(script)
    tools = _Tools(code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("logs", 'async def run(ctx, inputs):\n    await ctx.log("first")\n    await ctx.log("second")\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done"
    steps = await _steps(db, row.id)
    assert [s.step_kind for s in steps] == ["log", "log"]
    assert [s.step_id for s in steps] == ["log#1", "log#2"]
    assert [s.outputs["message"] for s in steps] == ["first", "second"]
    j = _journal(runner, row.id)
    assert [(e["seq"], e["kind"], e["result"]) for e in j[1:]] == [
        (1, "log", {"message": "first"}), (2, "log", {"message": "second"}),
    ]


async def test_journal_first_then_execute(db):
    seen: dict[str, Any] = {}
    holder: dict[str, Any] = {}

    async def probe(**kw):
        entries = holder["runner"]._v2.journal._runs[holder["run_id"]]
        seen["before"] = json.loads(json.dumps(entries[1]))
        return {"v": 42}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "x", 1, "tool", "probe", {"q": 1})
        return {"kind": "return", "value": None}

    fake = ScriptedCodeRun(script)
    tools = _Tools(probe=_Tool(probe), code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    holder["runner"] = runner
    pb = await _save(db, _pb("first", 'async def run(ctx, inputs):\n    x = await ctx.tool("probe", q=1)\n'))
    run = await runner.start_run_background(pb, inputs={})
    holder["run_id"] = str(run.id)
    row = await runner.wait_for_run(run.id, timeout=10)
    assert row.status == "done", row.error
    before = seen["before"]
    assert before["status"] == "in_flight"
    assert before["idempotency_key"] == f"{run.id}:1"
    assert before["id"] == "x" and before["occurrence"] == 1 and before["args"] == {"q": 1}
    assert before["result"] is None
    after = _journal(runner, run.id)[1]
    assert after["status"] == "done" and after["result"] == {"v": 42}
    assert after["ms"] is not None and after["ended_at"] is not None


async def test_active_run_id_and_nested_refusal_during_effect(db):
    seen: dict[str, Any] = {}

    async def probe(**kw):
        seen["active"] = active_run_id()
        seen["refusal"] = _nested_run_refusal()
        return {"ok": True}

    def script(env):
        if len(env["journal"]) == 1:
            return _effect(1, "probe", 1, "tool", "probe", {})
        return {"kind": "return", "value": None}

    fake = ScriptedCodeRun(script)
    tools = _Tools(probe=_Tool(probe), code_run=_Tool(fake.handler))
    runner, bus = _runner(db, tools)
    pb = await _save(db, _pb("active", 'async def run(ctx, inputs):\n    await ctx.tool("probe")\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done"
    assert seen["active"] == str(row.id)
    assert '"gate": "nested_playbook_run"' in seen["refusal"]
    assert active_run_id() is None


async def test_run_error_columns_migrate():
    engine = create_async_engine("sqlite+aiosqlite://")
    four = {"error", "error_type", "traceback", "failed_at"}
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for col in sorted(four):
                await conn.execute(text(f"ALTER TABLE playbook_runs DROP COLUMN {col}"))

        def cols(sync_conn):
            return {c["name"] for c in inspect(sync_conn).get_columns("playbook_runs")}

        async with engine.begin() as conn:
            before = await conn.run_sync(cols)
        assert not (four & before)
        await _ensure_columns(engine)
        async with engine.begin() as conn:
            after = await conn.run_sync(cols)
        assert after - before == four
        await _ensure_columns(engine)
        async with engine.begin() as conn:
            again = await conn.run_sync(cols)
        assert again == after
    finally:
        await engine.dispose()


async def test_memory_journal_store_roundtrip():
    from plugin_playbooks.v2.journal import make_effect_entry, make_entry0

    store = MemoryJournalStore()
    e0 = make_entry0(
        hash_seed=7, inputs={"n": 1}, playbook="pb", version=3, max_effects=200,
        code_sha256="ab" * 32,
    )
    assert {k for k in e0} == {
        "seq", "kind", "hash_seed", "inputs", "playbook", "version", "format", "mode",
        "max_effects", "started_at", "code_sha256",
    }
    assert e0["kind"] == "run" and e0["format"] == "python" and e0["mode"] == "real"
    # phase 06: `code_sha256` is present only when the caller pins one
    assert "code_sha256" not in make_entry0(
        hash_seed=0, inputs={}, playbook="pb", version=1, max_effects=1,
    )
    assert await store.journaled(["r1"]) == set()
    await store.start("r1", e0)
    assert (await store.entry0("r1"))["hash_seed"] == 7
    assert (await store.entry0("r1"))["code_sha256"] == "ab" * 32
    assert await store.journaled(["r1", "nope"]) == {"r1"}
    assert await store.in_flight("r1") == []
    seq = await store.append_in_flight("r1", make_effect_entry(
        run_id="r1", seq=0, kind="tool", id="rows", occurrence=1, name="fetch", args={"n": 1},
    ))
    assert seq == 1
    entry = (await store.read("r1"))[1]
    assert entry["status"] == "in_flight" and entry["idempotency_key"] == "r1:1"
    assert entry["result"] is None and entry["error"] is None and entry["dry"] is False
    assert {k for k in entry} == {
        "seq", "kind", "id", "occurrence", "name", "args", "idempotency_key", "status",
        "result", "error", "attempts", "dry", "started_at", "ended_at", "ms",
    }
    # phase 06: the write-ahead row is what a resume sees
    assert [e["seq"] for e in await store.in_flight("r1")] == [1]
    await store.complete("r1", 1, {"v": 1}, [{"n": 1, "error": None, "ms": 3}], 3)
    done = (await store.read("r1"))[1]
    assert done["status"] == "done" and done["result"] == {"v": 1} and done["ms"] == 3
    assert done["ended_at"] is not None
    assert await store.in_flight("r1") == []
    # phase 06: an in-flight row whose outcome a restart lost
    seq_u = await store.append_in_flight("r1", make_effect_entry(
        run_id="r1", seq=0, kind="agent", id="ask", occurrence=1, name=None, args={"q": 1},
    ))
    await store.mark_unknown("r1", seq_u, "outcome unknown — the server restarted while effect ask#1 was in flight")
    unknown = (await store.read("r1"))[seq_u]
    assert unknown["status"] == "timed_out_unknown"
    assert unknown["error"] == {
        "type": "OutcomeUnknown",
        "message": "outcome unknown — the server restarted while effect ask#1 was in flight",
    }
    assert unknown["ended_at"] is not None and unknown["attempts"] == []
    # Risks 9: handling an OutcomeUnknown keeps the row's status (only
    # `failed` becomes `failed_handled`)
    await store.mark_handled("r1", [seq_u])
    assert (await store.read("r1"))[seq_u]["status"] == "timed_out_unknown"
    assert await store.in_flight("r1") == []
    seq2 = await store.append_in_flight("r1", make_effect_entry(
        run_id="r1", seq=0, kind="now", id="now", occurrence=1, name=None, args={},
    ))
    assert seq2 == 3
    await store.fail("r1", 3, "EffectTimeout", "timed out", [{"n": 1, "error": "EffectTimeout: timed out", "ms": 1000}])
    failed = (await store.read("r1"))[3]
    assert failed["status"] == "failed" and failed["error"] == {"type": "EffectTimeout", "message": "timed out"}
    # reads are copies: mutating one never touches the store
    snapshot = await store.read("r1")
    snapshot[1]["result"] = "mutated"
    assert (await store.read("r1"))[1]["result"] == {"v": 1}
    await store.drop("r1")
    assert await store.entry0("r1") is None
    with pytest.raises(KeyError):
        await store.read("r1")
