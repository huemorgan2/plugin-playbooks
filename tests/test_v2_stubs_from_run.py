"""plans/032 phase 08 (part 1) — `playbook_dry_run(stubs_from_run=<run_id>)`:
a recorded run's REAL effect results replayed as per-occurrence stubs
against the candidate (docs/v2.md §10). The replay is a simulation — it
never counts as run evidence.

Real-jail cases: the failure → fix → replay loop is `run()` code semantics
(a `try`/`except ctx.ToolError` around the replayed failure). Skipped
without a usable jail.
"""

from __future__ import annotations

import json
import uuid

from readstage import parse_read_stage
from sqlalchemy import func, select
from v2harness import CODE, env

from _jail import real_code_run, real_jail, requires_jail
from plugin_playbooks.models import PlaybookJournal, PlaybookRun

BROKEN = '''async def run(ctx, inputs):
    a = await ctx.tool("echo", message="one", _id="a")
    b = await ctx.tool("boom", _id="b")
    return {"a": a, "b": b}
'''

FIXED = '''async def run(ctx, inputs):
    a = await ctx.tool("echo", message="one", _id="a")
    try:
        b = await ctx.tool("boom", _id="b")
    except ctx.ToolError as e:
        b = "handled: " + str(e)
    c = await ctx.tool("echo", message="three", _id="c")
    return {"a": a, "b": b, "c": c}
'''

PY_GREETER = (
    "async def run(ctx, inputs):\n"
    "    say = await ctx.tool('echo', message=inputs['greeting'])\n"
    "    return say\n"
)


async def _echo(**kw):
    return kw


async def _boom(**kw):
    raise RuntimeError("kaboom")


async def _jail_env(tmp_path, **tools):
    e = await env(echo=_echo, boom=_boom, **tools)
    e.registry.add("code_run", real_code_run(tmp_path))
    return e


async def _propose(e, name, code):
    out = json.loads(await e.tools["playbook_propose"](name=name, code=code))
    assert out["status"] == "candidate_saved", out
    return out


async def _edit(e, name, code):
    read = parse_read_stage(await e.tools["playbook_edit"](name=name))
    out = json.loads(await e.tools["playbook_edit"](name=name, ticket=read["ticket"], code=code))
    assert out["status"] == "candidate_saved", out
    return out


async def _candidate_run(e, name, inputs="{}"):
    out = json.loads(await e.tools["playbook_run_candidate"](name=name, inputs=inputs, wait_seconds=30))
    assert out["status"] in ("done", "failed"), out
    return out


async def _counts(sf) -> tuple[int, int]:
    async with sf() as s:
        runs = (await s.execute(select(func.count()).select_from(PlaybookRun))).scalar_one()
        rows = (await s.execute(select(func.count()).select_from(PlaybookJournal))).scalar_one()
    return runs, rows


# ------------------------------------------------------------------ 1
@real_jail
@requires_jail()
async def test_failed_run_journal_reaches_the_corrected_line(tmp_path):
    e = await _jail_env(tmp_path)
    try:
        await _propose(e, "py", BROKEN)
        failed = await _candidate_run(e, "py")
        assert failed["status"] == "failed" and failed["error_type"] == "ToolError", failed
        run_id = failed["run_id"]
        await _edit(e, "py", FIXED)
        dry = json.loads(await e.tools["playbook_dry_run"](
            name="py", version="candidate", stubs_from_run=run_id,
        ))
        assert dry["status"] == "simulated" and dry["dry_run"] is True, dry
        assert dry["error"] is None and dry["tested_version"] == 2 and dry["is_candidate"] is True
        ran = dry["steps_ran"]
        # a#1 replays the recorded result, b#1 the recorded failure (caught
        # by the corrected line), c#1 — never recorded — is a placeholder
        assert ran["a#1"]["stubbed"] is True and ran["a#1"]["result"] == {"message": "one"}
        assert ran["b#1"]["stubbed"] is True
        assert ran["c#1"]["stubbed"] is False
        assert dry["result"]["a"] == {"message": "one"}
        assert dry["result"]["b"].startswith("handled: ") and "kaboom" in dry["result"]["b"]
        assert dry["result"]["c"] == "<dry:c#1>"
        j = {f"{x['id']}#{x['occurrence']}": x for x in dry["journal"][1:]}
        assert j["b#1"]["status"] == "failed_handled" and j["b#1"]["error"]["type"] == "ToolError"
        src = dry["stubs_source"]
        assert src == {
            "run_id": run_id, "version": 1, "format": "python", "status": "failed",
            "occurrences_used": ["a#1", "b#1"], "occurrences_unmatched": [],
        }
        assert "SIMULATED" in dry["banner"]
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 2
@real_jail
@requires_jail()
async def test_recorded_failure_replays_as_the_error(tmp_path):
    e = await _jail_env(tmp_path)
    try:
        await _propose(e, "py", BROKEN)
        failed = await _candidate_run(e, "py")
        assert failed["status"] == "failed", failed
        # the same (unfixed) code: the replayed failure is the run's failure
        dry = json.loads(await e.tools["playbook_dry_run"](
            name="py", version="candidate", stubs_from_run=failed["run_id"],
        ))
        assert dry["dry_run"] is True and dry["error_type"] == "ToolError", dry
        assert "kaboom" in dry["error"] and dry["result"] is None
        assert dry["steps_ran"]["b#1"]["stubbed"] is True
        assert dry["stubs_source"]["occurrences_used"] == ["a#1", "b#1"]
        # an explicit `_raise` stub is the same thing, spelled by hand
        by_hand = json.loads(await e.tools["playbook_dry_run"](
            name="py", version="candidate",
            stubs=json.dumps({"b#1": {"_raise": {"type": "EffectTimeout", "message": "slow"}}}),
        ))
        assert by_hand["error_type"] == "EffectTimeout" and "slow" in by_hand["error"]
        assert "stubs_source" not in by_hand
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 3
@real_jail
@requires_jail()
async def test_pblang_run_rows_feed_a_python_candidate(tmp_path):
    e = await _jail_env(tmp_path)
    try:
        await _propose(e, "greeter", CODE)
        v1 = await _candidate_run(e, "greeter", inputs='{"greeting": "hi"}')
        assert v1["status"] == "done", v1
        assert v1["step_results"]["say"] == {"tool": "echo", "result": {"message": "hi"}}
        out = await _edit(e, "greeter", PY_GREETER)
        assert out["format"] == "python" and out["candidate_version"] == 2
        dry = json.loads(await e.tools["playbook_dry_run"](
            name="greeter", version="candidate", inputs='{"greeting": "yo"}',
            stubs_from_run=v1["run_id"],
        ))
        assert dry["status"] == "simulated" and dry["format"] == "python", dry
        # the v1 step row `say` (tool echo) answers the python call site
        # `say` (the assigned name) — the recorded result, not the new input
        assert dry["steps_ran"]["say#1"]["stubbed"] is True
        assert dry["steps_ran"]["say#1"]["result"] == {"message": "hi"}
        assert dry["result"] == {"message": "hi"}
        src = dry["stubs_source"]
        assert (src["run_id"], src["version"], src["format"], src["status"]) == (v1["run_id"], 1, "pblang", "done")
        assert src["occurrences_used"] == ["say#1"]
        assert src["occurrences_unmatched"] == ["echo#1"]
        # and a pblang target reads the same rows by bare step id / tool name
        dry_pb = json.loads(await e.tools["playbook_dry_run"](
            name="greeter", version="1", inputs='{"greeting": "yo"}',
            stubs_from_run=v1["run_id"],
        ))
        assert dry_pb["format"] == "pblang" and dry_pb["dry_run"] is True, dry_pb
        assert dry_pb["stubs_source"]["occurrences_used"] == ["say#1", "say", "echo#1", "echo"]
        say = next(t for t in dry_pb["trace"] if isinstance(t, dict) and t.get("step_id") == "say")
        assert say["output"]["stubbed"] is True and say["output"]["result"] == {"message": "hi"}
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 4
@real_jail
@requires_jail()
async def test_explicit_stubs_win_and_wrong_playbook_refused(tmp_path):
    e = await _jail_env(tmp_path)
    try:
        await _propose(e, "py", BROKEN)
        await _propose(e, "other", CODE.replace("greeter", "other"))
        failed = await _candidate_run(e, "py")
        other = await _candidate_run(e, "other", inputs='{"greeting": "x"}')
        await _edit(e, "py", FIXED)
        # explicit stubs override the recorded value key by key
        dry = json.loads(await e.tools["playbook_dry_run"](
            name="py", version="candidate", stubs_from_run=failed["run_id"],
            stubs=json.dumps({"a#1": "override", "b#1": {"fine": True}}),
        ))
        assert dry["status"] == "simulated", dry
        assert dry["result"] == {"a": "override", "b": {"fine": True}, "c": "<dry:c#1>"}
        # another playbook's run, an unknown run, a malformed id: refused,
        # nothing simulated
        for rid, needle in (
            (other["run_id"], "belongs to playbook 'other'"),
            (str(uuid.uuid4()), "not found"),
            ("nope", "not found"),
        ):
            out = json.loads(await e.tools["playbook_dry_run"](
                name="py", version="candidate", stubs_from_run=rid,
            ))
            assert needle in out["error"] and out["dry_run"] is True, out
            assert "steps_ran" not in out
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 5
@real_jail
@requires_jail()
async def test_never_evidence(tmp_path):
    e = await _jail_env(tmp_path)
    try:
        await _propose(e, "py", BROKEN)
        failed = await _candidate_run(e, "py")
        await _edit(e, "py", FIXED)
        before = await _counts(e.sf)
        dry = json.loads(await e.tools["playbook_dry_run"](
            name="py", version="candidate", stubs_from_run=failed["run_id"],
        ))
        assert dry["status"] == "simulated" and dry["error"] is None, dry
        # no run row, no journal row
        assert await _counts(e.sf) == before
        # the publish gate still wants a REAL run of the candidate
        out = json.loads(await e.tools["playbook_publish"](name="py"))
        assert out.get("status") != "published"
        assert out["gate"] == "test_run" and "Dry runs are not run evidence" in out["error"], out
        assert "never counts as run evidence" in e.defs["playbook_dry_run"].description
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 6
@real_jail
@requires_jail()
async def test_status_hint_names_stubs_from_run(tmp_path):
    e = await _jail_env(tmp_path)
    try:
        await _propose(e, "py", BROKEN)
        failed = await _candidate_run(e, "py")
        st = json.loads(await e.tools["playbook_status"](run_id=failed["run_id"]))
        assert st["status"] == "failed" and st["error_type"] == "ToolError", st
        assert (
            f"playbook_dry_run(name='py', version='candidate', stubs_from_run='{failed['run_id']}')"
            in st["hint"]
        )
        assert "stubs_from_run" in e.defs["playbook_dry_run"].parameters["properties"]
    finally:
        await e.dispose()
