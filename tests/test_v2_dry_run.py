"""plans/032 phase 05 — dry run on the segment loop (docs/v2.md §10).

The jail cases run the real shim through plugin-inline-code-run (skipped
when the jail is unavailable); the intake and dispatch cases go through
`playbook_dry_run` built by `tests/v2harness.py`.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from plugin_playbooks.models import PlaybookJournal, PlaybookRun, PlaybookStepRun
from plugin_playbooks.v2 import MemoryJournalStore
from plugin_playbooks.v2.loop import SegmentLoop
from _jail import real_code_run, real_jail, requires_jail
from test_v2_loop import _pb, _real_tools, _runner, db  # noqa: F401 — fixture
from v2harness import CODE, env


def _dry_loop(tmp_path) -> SegmentLoop:
    return SegmentLoop(None, _real_tools(tmp_path), None, None, MemoryJournalStore(keep_completed=True))


UNREACHED = '''async def run(ctx, inputs):
    rows = await ctx.tool("fetch", _id="fetch")
    for r in rows:
        await ctx.tool("send", to=r["email"], _id="send")
    if False:
        await ctx.tool("never", _id="never")
    return len(rows)
'''

TOTAL = '''async def run(ctx, inputs):
    rows = await ctx.tool("fetch", _id="fetch")
    return rows["total"]
'''

PAGES = '''async def run(ctx, inputs):
    out = []
    for i in range(3):
        p = await ctx.tool("page", i=i, _id="page")
        out.append(p)
    return out
'''

EARLY = '''async def run(ctx, inputs):
    return 1
    await ctx.tool("x", _id="x")
    await ctx.llm("p", _id="y")
'''

KINDS = '''async def run(ctx, inputs):
    a = await ctx.approve(show="hi")
    t = await ctx.now()
    r = await ctx.random()
    await ctx.log("m")
    return {"a": a, "t": t.isoformat(), "r": r}
'''

SEMANTICS = '''async def run(ctx, inputs):
    a = await ctx.tool("a", _id="a")
    b = await ctx.tool("b", _id="b")
    seen = [a]
    out = {
        "truthy": bool(a),
        "once": sum(1 for _ in a),
        "nested": str(a["x"]["y"]),
        "gt": a["n"] > 3,
        "contains": "k" in a,
        "eq_each_other": a == b,
        "eq_real": a == {"x": 1},
        "not_in_seen": b not in seen,
    }
    try:
        a["n"] + 1
    except Exception as e:
        out["arith"] = type(e).__name__
    return out
'''

COUNT = '''async def run(ctx, inputs):
    await ctx.tool("echo", n=inputs["count"], _id="echo")
    return inputs["count"]
'''


@real_jail
@requires_jail()
async def test_drystub_semantics(tmp_path):
    res = await _dry_loop(tmp_path).dry_run(_pb("sem", SEMANTICS), {}, {}, version=1)
    assert res["status"] == "simulated", res
    assert res["error"] is None, res["error"]
    out = res["result"]
    assert out["truthy"] is True and out["once"] == 1
    assert out["nested"] == "<dry:a#1.x.y>"
    assert out["gt"] is True and out["contains"] is True
    assert out["eq_each_other"] is True and out["eq_real"] is False
    assert out["not_in_seen"] is False
    assert out["arith"] == "DryStubError"


@real_jail
@requires_jail()
async def test_unstubbed_loop_runs_once_and_reports_unreached(tmp_path):
    res = await _dry_loop(tmp_path).dry_run(_pb("u", UNREACHED), {}, {}, version=1)
    assert res["status"] == "simulated"
    assert res["dry_run"] is True and "SIMULATED" in res["banner"]
    assert set(res["steps_ran"]) == {"fetch#1", "send#1"}
    assert res["steps_ran"]["fetch#1"]["stubbed"] is False
    assert res["steps_ran"]["send#1"]["args"] == {"to": "<dry:fetch#1[0].email>"}
    assert [u["id"] for u in res["unreached_call_sites"]] == ["never"]
    assert res["unreached_call_sites"][0]["kind"] == "tool"
    assert res["unreached_call_sites"][0]["line"] == 6
    assert res["result"] == 1


@real_jail
@requires_jail()
async def test_drystub_error_names_effect_path_and_key(tmp_path):
    res = await _dry_loop(tmp_path).dry_run(
        _pb("t", TOTAL), {}, {"fetch#1": {"items": []}}, version=1,
    )
    assert res["status"] == "simulated"
    assert res["error_type"] == "DryStubError"
    err = res["error"]
    assert "fetch#1" in err and "total" in err and 'stubs={"fetch#1"' in err
    assert "Traceback" not in json.dumps(res)
    assert res["steps_ran"]["fetch#1"]["stubbed"] is True


@real_jail
@requires_jail()
async def test_stubs_per_occurrence(tmp_path):
    res = await _dry_loop(tmp_path).dry_run(
        _pb("p", PAGES), {}, {"page#2": {"n": 2}}, version=1,
    )
    assert res["status"] == "simulated" and res["error"] is None
    ran = res["steps_ran"]
    assert list(ran) == ["page#1", "page#2", "page#3"]
    assert ran["page#2"]["stubbed"] is True and ran["page#2"]["result"] == {"n": 2}
    assert ran["page#1"]["stubbed"] is False and ran["page#3"]["stubbed"] is False
    assert ran["page#1"]["result"] is None and ran["page#3"]["result"] is None
    assert res["result"] == ["<dry:page#1>", {"n": 2}, "<dry:page#3>"]
    assert res["unreached_call_sites"] == []


@real_jail
@requires_jail()
async def test_status_simulated_nothing_exercised(tmp_path):
    res = await _dry_loop(tmp_path).dry_run(_pb("e", EARLY), {}, {}, version=1)
    assert res["status"] == "simulated_nothing_exercised"
    assert res["steps_ran"] == {} and res["result"] == 1
    assert [(u["id"], u["kind"]) for u in res["unreached_call_sites"]] == [("x", "tool"), ("y", "llm")]


@real_jail
@requires_jail()
async def test_dry_run_writes_no_run_rows(db, tmp_path):  # noqa: F811
    runner, _bus = _runner(db, _real_tools(tmp_path))
    res = await runner._v2.dry_run(_pb("u", UNREACHED), {}, {}, version=1)
    assert res["status"] == "simulated"
    async with db() as s:
        runs = (await s.execute(select(func.count()).select_from(PlaybookRun))).scalar_one()
        steps = (await s.execute(select(func.count()).select_from(PlaybookStepRun))).scalar_one()
    assert runs == 0 and steps == 0
    journal = res["journal"]
    assert journal[0]["mode"] == "dry" and journal[0]["hash_seed"] == 0
    assert len(journal) == 3
    assert all(e["dry"] is True for e in journal[1:])
    # the live loop's own journal is untouched by a dry run — neither the
    # test double (memory) nor the durable table (phase 06) holds a row
    assert runner._v2.journal._runs == {}
    assert await _journal_rows(db) == 0


async def _journal_rows(sf) -> int:
    async with sf() as s:
        return (await s.execute(select(func.count()).select_from(PlaybookJournal))).scalar_one()


@real_jail
@requires_jail()
async def test_per_kind_answers(tmp_path):
    loop = _dry_loop(tmp_path)
    r1 = await loop.dry_run(_pb("k", KINDS), {}, {}, version=1)
    r2 = await loop.dry_run(_pb("k", KINDS), {}, {}, version=1)
    assert r1["status"] == "simulated" and r1["error"] is None, r1["error"]
    a = r1["result"]["a"]
    assert a["approved"] is True and a["dry"] is True
    # site ids: assignment target (`a`, `t`, `r`), else the kind (`log`)
    assert a["request_id"] == "dry:a#1"
    assert a["reason"] is None and a["decided_by"] is None
    assert r1["result"]["t"] == "2000-01-01T00:00:00+00:00"
    assert isinstance(r1["result"]["r"], float) and 0 <= r1["result"]["r"] < 1
    assert r1["result"] == r2["result"]
    assert set(r1["steps_ran"]) == {"a#1", "t#1", "r#1", "log#1"}


async def _echo(message: str = "") -> str:
    return message


async def _python_env(tmp_path, code, **propose):
    e = await env(echo=_echo)
    e.registry.add("code_run", real_code_run(tmp_path))
    out = json.loads(await e.tools["playbook_propose"](name="py", code=code, **propose))
    assert out["status"] == "candidate_saved", out
    return e


@real_jail
@requires_jail()
@pytest.mark.parametrize("declared", ["integer", "number"])
async def test_dry_intake_coerces_and_fails_loud(tmp_path, declared):
    schema = json.dumps({"type": "object", "properties": {"count": {"type": declared}}})
    e = await _python_env(tmp_path, COUNT, inputs_schema=schema)
    try:
        good = json.loads(await e.tools["playbook_dry_run"](name="py", inputs='{"count": "4"}'))
        assert good["status"] == "simulated", good
        assert good["journal"][0]["inputs"]["count"] == 4
        assert good["steps_ran"]["echo#1"]["args"] == {"n": 4}
        assert good["result"] == 4
        bad = json.loads(await e.tools["playbook_dry_run"](name="py", inputs='{"count": "abc"}'))
        assert bad["status"] == "rejected" and bad["dry_run"] is True
        assert bad["input"] == "count" and bad["expected"] == declared
        assert "count" in bad["error"]
        # phase 06: the default store is durable — a dry run writes no row
        assert "journal" not in bad and await _journal_rows(e.sf) == 0
    finally:
        await e.dispose()


@real_jail
@requires_jail()
async def test_tool_dispatch_by_format(tmp_path):
    e = await _python_env(tmp_path, UNREACHED)
    try:
        await e.tools["playbook_propose"](name="greeter", code=CODE)
        py = json.loads(await e.tools["playbook_dry_run"](name="py"))
        assert py["format"] == "python" and py["dry_run"] is True
        assert py["status"] == "simulated" and set(py["steps_ran"]) == {"fetch#1", "send#1"}
        assert py["tested_version"] == 1 and py["is_candidate"] is True
        pb = json.loads(await e.tools["playbook_dry_run"](name="greeter"))
        assert pb["format"] == "pblang" and pb["dry_run"] is True
        assert "trace" in pb and "references" in pb
        assert "steps_ran" not in pb and "unreached_call_sites" not in pb
    finally:
        await e.dispose()
