"""plans/032 phase 08 (part 1) — `playbook_runs.result`: what a python
playbook's `run()` returned, persisted on the run row, the additive LAST key
of `playbook.run.completed`, and surfaced on every reader (docs/v2.md §7).
A v1 run's result is null everywhere; a non-JSON return fails loud; a
resolved vault value never lands in the column; `ctx.subtask` returns the
child's PERSISTED result.
"""

from __future__ import annotations

import json
import uuid

from evidence import EXPLANATION, green_run
from sqlalchemy import select
from test_v2_effects import CHILD_DOUBLE, _runner as _fx_runner, _tools as _fx_tools
from test_v2_loop import (
    ScriptedCodeRun, _Ctx, _effect, _pb, _run_to_end, _runner, _save, _Tool,
    _Tools, _Vault, db,  # noqa: F401 — fixture
)
from test_wake_on_completion import _Bus as _WakeBus, _Ctx as _WakeCtx, _drain, _payload
from v2harness import CODE, env

from _jail import real_jail, requires_jail
from plugin_playbooks import routes
from plugin_playbooks.models import PlaybookRun
from plugin_playbooks.wake import RunCompletionWake

PY_GREETER = (
    "async def run(ctx, inputs):\n"
    "    say = await ctx.tool('echo', message=inputs['greeting'])\n"
    "    return {'said': say, 'n': 2}\n"
)
VALUE = {"said": {"message": "hi"}, "n": 2}
COMPLETED_KEYS = [
    "run_id", "status", "duration_ms", "error", "playbook_id", "playbook_version",
    "is_test", "playbook_name", "trigger", "conversation_id", "parent_run_id",
    "wake_on_complete", "result",
]


async def _echo(**kw):
    return kw


def _greeter_script(envelope):
    if len(envelope["journal"]) == 1:
        return _effect(1, "say", 1, "tool", "echo", {"message": envelope["journal"][0]["inputs"]["greeting"]})
    return {"kind": "return", "value": {"said": envelope["journal"][1]["result"], "n": 2}}


async def _run_row(sf, run_id) -> PlaybookRun:
    async with sf() as s:
        return await s.get(PlaybookRun, uuid.UUID(run_id))


# ------------------------------------------------------------------ 1
async def test_result_persisted_and_surfaced():
    e = await env(script=_greeter_script, echo=_echo)
    try:
        out = json.loads(await e.tools["playbook_propose"](
            name="py", code=PY_GREETER, agent_autonomy="agent_may_trigger",
        ))
        assert out["status"] == "candidate_saved", out
        cand = json.loads(await e.tools["playbook_run_candidate"](
            name="py", inputs='{"greeting": "hi"}', wait_seconds=10,
        ))
        assert cand["status"] == "done" and cand["result"] == VALUE, cand
        # the row
        row = await _run_row(e.sf, cand["run_id"])
        assert row.result == VALUE and row.format == "python"
        # the event: the v1 keys in order, `result` last
        payload = e.bus.named("playbook.run.completed")[-1]
        assert list(payload) == COMPLETED_KEYS and payload["result"] == VALUE
        # playbook_status / playbook_runs
        st = json.loads(await e.tools["playbook_status"](run_id=cand["run_id"]))
        assert st["status"] == "done" and st["result"] == VALUE and "error" not in st
        runs = json.loads(await e.tools["playbook_runs"](name="py"))
        assert runs["runs"][0]["result"] == VALUE and runs["runs"][0]["format"] == "python"
        # the REST readers
        routes.init_routes(e.sf, e.runner)
        listed = await routes.list_runs("py")
        assert listed[0]["result"] == VALUE and listed[0]["format"] == "python"
        got = await routes.get_run(cand["run_id"])
        assert got["result"] == VALUE and got["format"] == "python"
        # the live path (playbook_run) after publish
        await green_run(e.sf, 1, name="py")
        assert json.loads(await e.tools["playbook_publish"](name="py", explanation=EXPLANATION))["status"] == "published"
        live = json.loads(await e.tools["playbook_run"](
            name="py", inputs='{"greeting": "hi"}', wait_seconds=10,
        ))
        assert live["status"] == "done" and live["result"] == VALUE, live
        assert "warning" not in live
        # the wake moment leads with the result
        wctx = _WakeCtx()
        svc = RunCompletionWake(e.sf, _WakeBus(), wctx)
        origin = uuid.uuid4()
        await svc._on_completed(_payload(
            run_id=live["run_id"], playbook_name="py", wake_on_complete=True,
            conversation_id=str(origin), result=VALUE,
        ))
        await _drain(svc)
        assert len(wctx.sent) == 1
        content = wctx.sent[0]["content"]
        assert "Result:\n" in content and '"said"' in content
        assert content.index("Result:") < content.index("Step outputs:")
        assert "produced no step outputs" not in content
        # the tool descriptions name it
        for name in ("playbook_run", "playbook_run_candidate", "playbook_status", "playbook_runs"):
            assert "`result`" in e.defs[name].description, name
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 2
async def test_v1_result_is_null_everywhere():
    e = await env(echo=_echo)
    try:
        out = json.loads(await e.tools["playbook_propose"](
            name="greeter", code=CODE, agent_autonomy="agent_may_trigger",
        ))
        assert out["status"] == "candidate_saved", out
        cand = json.loads(await e.tools["playbook_run_candidate"](
            name="greeter", inputs='{"greeting": "hi"}', wait_seconds=10,
        ))
        assert cand["status"] == "done", cand
        assert "result" in cand and cand["result"] is None
        assert cand["step_results"]["say"]["tool"] == "echo"
        row = await _run_row(e.sf, cand["run_id"])
        assert row.result is None and row.format == "pblang"
        payload = e.bus.named("playbook.run.completed")[-1]
        assert list(payload) == COMPLETED_KEYS and payload["result"] is None
        st = json.loads(await e.tools["playbook_status"](run_id=cand["run_id"]))
        assert "result" in st and st["result"] is None
        runs = json.loads(await e.tools["playbook_runs"](name="greeter"))
        assert runs["runs"][0]["result"] is None and runs["runs"][0]["format"] == "pblang"
        routes.init_routes(e.sf, e.runner)
        assert (await routes.list_runs("greeter"))[0]["result"] is None
        assert (await routes.get_run(cand["run_id"]))["result"] is None
        await green_run(e.sf, 1)
        assert json.loads(await e.tools["playbook_publish"](name="greeter", explanation=EXPLANATION))["status"] == "published"
        live = json.loads(await e.tools["playbook_run"](
            name="greeter", inputs='{"greeting": "hi"}', wait_seconds=10,
        ))
        assert live["status"] == "done" and live["result"] is None, live
        assert live["step_results"]["say"]["tool"] == "echo" and "warning" not in live
        # the wake keeps its v1 wording: step outputs, no Result block
        wctx = _WakeCtx()
        svc = RunCompletionWake(e.sf, _WakeBus(), wctx)
        await svc._on_completed(_payload(
            run_id=live["run_id"], playbook_name="greeter", wake_on_complete=True,
            conversation_id=str(uuid.uuid4()),
        ))
        await _drain(svc)
        content = wctx.sent[0]["content"]
        assert "Result:" not in content and "Step outputs:" in content
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 3
@real_jail
@requires_jail()
async def test_non_json_return_fails_loud(db, tmp_path):
    runner, bus = _fx_runner(db, _fx_tools(tmp_path))
    pb = await _save(db, _pb("notjson", "async def run(ctx, inputs):\n    return {1, 2}\n"))
    row = await _run_to_end(runner, pb, timeout=60.0)
    assert row.status == "failed", (row.status, row.error)
    assert row.error_type == "TypeError"
    assert "return value is not JSON: set" in (row.error or "")
    assert row.result is None
    payload = [p for n, p in bus.events if n == "playbook.run.completed"][-1]
    assert list(payload) == COMPLETED_KEYS
    assert payload["status"] == "failed" and payload["result"] is None


# ------------------------------------------------------------------ 4
async def test_resolved_secret_never_stored(db):
    async def http(**kw):
        # a tool that leaks what it was given (the resolved secret)
        return {"echoed": kw["headers"]["x-api-key"], "status": 200}

    def script(envelope):
        if len(envelope["journal"]) == 1:
            return _effect(1, "resp", 1, "tool", "http", {"headers": {"x-api-key": "vault:my_key"}})
        # run() returns the tool result verbatim — the secret is inside it
        return {"kind": "return", "value": {"resp": envelope["journal"][1]["result"], "k": "s3cret-value"}}

    fake = ScriptedCodeRun(script)
    tools = _Tools(http=_Tool(http), code_run=_Tool(fake.handler))
    vault = _Vault({"my_key": "s3cret-value"})
    runner, bus = _runner(db, tools, context=_Ctx(vault))
    pb = await _save(db, _pb("leaky", 'async def run(ctx, inputs):\n    resp = await ctx.tool("http", headers={"x-api-key": "vault:my_key"})\n    return {"resp": resp, "k": "x"}\n'))
    row = await _run_to_end(runner, pb)
    assert row.status == "done", row.error
    assert vault.reads == ["my_key"]
    assert row.result == {"resp": {"echoed": "vault:my_key", "status": 200}, "k": "vault:my_key"}
    payload = bus.named("playbook.run.completed")[-1]
    assert payload["result"] == row.result
    assert "s3cret-value" not in json.dumps({"row": row.result, "event": payload}, default=str)


# ------------------------------------------------------------------ 5
@real_jail
@requires_jail()
async def test_subtask_returns_persisted_result(db, tmp_path):
    runner, bus = _fx_runner(db, _fx_tools(tmp_path))
    await _save(db, _pb("child", CHILD_DOUBLE))
    parent_pb = await _save(db, _pb(
        "parent", 'async def run(ctx, inputs):\n    got = await ctx.subtask("child", {"n": 3})\n    return {"child": got}\n',
    ))
    parent = await _run_to_end(runner, parent_pb, timeout=60.0)
    assert parent.status == "done", (parent.error, parent.traceback)
    async with db() as s:
        rows = list((await s.execute(select(PlaybookRun))).scalars().all())
    child = next(r for r in rows if r.id != parent.id)
    assert child.parent_run_id == parent.id and child.status == "done"
    assert child.result == {"n": 6} and child.format == "python"
    assert parent.result == {"child": child.result}
    assert runner._v2._values == {}  # the in-memory value was consumed
    # the persisted column is what ctx.subtask hands back; an unknown or
    # unfinished child falls back to the in-memory value
    assert await runner._v2._persisted_result(child.id, "fallback") == {"n": 6}
    assert await runner._v2._persisted_result(uuid.uuid4(), "fallback") == "fallback"
    done = {p["run_id"]: p for n, p in bus.events if n == "playbook.run.completed"}
    assert done[str(child.id)]["result"] == {"n": 6}
    assert done[str(parent.id)]["result"] == {"child": {"n": 6}}
