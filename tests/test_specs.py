"""0.11.0 (plans/002 phase 4) — specs: fixture simulation with assertions.

Stub seam in dry_run, SpecDef/evaluator semantics, spec tools, auto-run on
candidate save, and the specs promote gate.
"""

from __future__ import annotations

import json

import pytest

from evidence import EXPLANATION, green_run, make_plan
from readstage import parse_read_stage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookSpec, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.specs import SpecDef, evaluate_spec, parse_spec_yaml


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

    def names(self):
        return list(self._tools)


async def _noop(**_kw):
    return {"ok": True}


class _ChatCtx:
    """plans/016 phase 2: agent-invoked runs report to their origin chat —
    real Luna always pins the conversation contextvar for chat turns, so the
    fixture must too (a run with no chat now refuses implicit sends)."""

    def __init__(self) -> None:
        import uuid
        self.current_conversation_id = uuid.uuid4()


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
        context=_ChatCtx(),
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


# ---- stub seam -------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_stub_by_step_id_and_tool_name(env):
    sf, runner, handlers, _ = env
    pb = Playbook(
        name="p", display_name="p", status="enabled",
        definition={"name": "p", "steps": [
            {"id": "fetch", "kind": "tool_call", "tool": "t", "args": {}},
            {"id": "say", "kind": "tool_call", "tool": "send_chat_message",
             "args": {"message": "{{ steps.fetch.result.city }}"}},
        ]},
    )
    out = await runner.dry_run(pb, stubs={"fetch": {"city": "Haifa"}})
    assert out["status"] == "done"
    fetch = out["trace"][0]["output"]
    assert fetch["stubbed"] is True
    assert fetch["result"] == {"city": "Haifa"}
    # the stubbed value flowed into the downstream template
    assert out["trace"][1]["output"]["resolved_args"] == {"message": "Haifa"}

    # tool-name key works too; step-id wins when both are present
    out2 = await runner.dry_run(pb, stubs={"t": {"city": "Oslo"}})
    assert out2["trace"][0]["output"]["result"] == {"city": "Oslo"}
    out3 = await runner.dry_run(
        pb, stubs={"t": {"city": "Oslo"}, "fetch": {"city": "Rome"}},
    )
    assert out3["trace"][0]["output"]["result"] == {"city": "Rome"}


@pytest.mark.asyncio
async def test_dry_run_stubs_agent_and_llm_steps(env):
    sf, runner, handlers, _ = env
    pb = Playbook(
        name="p2", display_name="p2", status="enabled",
        definition={"name": "p2", "steps": [
            {"id": "judge", "kind": "llm_step", "prompt": "classify",
             "output_schema": {"label": "string"}},
            {"id": "act", "kind": "tool_call", "tool": "t",
             "args": {"v": "{{ steps.judge.label }}"}},
        ]},
    )
    out = await runner.dry_run(pb, stubs={"judge": {"label": "urgent"}})
    assert out["trace"][0]["output"] == {"label": "urgent"}
    assert out["trace"][1]["output"]["resolved_args"] == {"v": "urgent"}
    # without a stub the schema placeholder is used
    out2 = await runner.dry_run(pb)
    assert out2["trace"][0]["output"].get("label") != "urgent"


# ---- SpecDef + evaluator ---------------------------------------------------

def test_parse_spec_yaml_rejects_bad_documents():
    with pytest.raises(ValueError, match="YAML mapping"):
        parse_spec_yaml("- a\n- b\n")
    with pytest.raises(ValueError, match="invalid"):
        parse_spec_yaml("expect:\n  status: maybe\n")
    with pytest.raises(ValueError, match="invalid"):
        parse_spec_yaml("unknown_key: 1\n")


def _dry(status="done", trace=None, error=None):
    return {"status": status, "error": error, "trace": trace or []}


def _tool_entry(step_id, tool, args=None, result=None):
    return {"step_id": step_id, "kind": "tool_call",
            "output": {"tool": tool, "resolved_args": args or {},
                       "result": result, "_dry": True}}


def test_evaluator_status_and_error():
    spec = SpecDef.model_validate({"expect": {"status": "done"}})
    assert evaluate_spec(spec, _dry())["passed"]
    res = evaluate_spec(spec, _dry(status="failed", error="boom"))
    assert not res["passed"]
    assert "expected status 'done'" in res["failures"][0]
    assert "boom" in res["failures"][0]

    spec2 = SpecDef.model_validate(
        {"expect": {"status": "failed", "error_contains": "boom"}}
    )
    assert evaluate_spec(spec2, _dry(status="failed", error="kaboom"))["passed"]
    res2 = evaluate_spec(spec2, _dry(status="failed", error="other"))
    assert "expected error to contain 'boom'" in res2["failures"][0]


def test_evaluator_step_order_and_not_ran():
    trace = [_tool_entry("a", "t"), _tool_entry("b", "t"), _tool_entry("c", "t")]
    ok = SpecDef.model_validate({"expect": {"steps_ran": ["a", "c"]}})
    assert evaluate_spec(ok, _dry(trace=trace))["passed"]
    wrong = SpecDef.model_validate({"expect": {"steps_ran": ["c", "a"]}})
    res = evaluate_spec(wrong, _dry(trace=trace))
    assert "in order" in res["failures"][0]
    not_ran = SpecDef.model_validate({"expect": {"steps_not_ran": ["b"]}})
    res2 = evaluate_spec(not_ran, _dry(trace=trace))
    assert "NOT to run" in res2["failures"][0]


def test_evaluator_tool_calls_count_and_args():
    trace = [
        _tool_entry("s1", "send_chat_message", {"message": "hello Roy"}),
        _tool_entry("s2", "send_chat_message", {"message": "bye"}),
    ]
    spec = SpecDef.model_validate({"expect": {"tool_calls": {
        "send_chat_message": {"count": 2, "args_contain": {"message": "Roy"}},
    }}})
    assert evaluate_spec(spec, _dry(trace=trace))["passed"]

    bad_count = SpecDef.model_validate({"expect": {"tool_calls": {
        "send_chat_message": {"count": 1},
    }}})
    res = evaluate_spec(bad_count, _dry(trace=trace))
    assert "called 1x, saw 2" in res["failures"][0]

    missing = SpecDef.model_validate({"expect": {"tool_calls": {
        "send_email": {"count": 1},
    }}})
    res2 = evaluate_spec(missing, _dry(trace=trace))
    assert "called 1x, saw 0" in res2["failures"][0]

    bad_args = SpecDef.model_validate({"expect": {"tool_calls": {
        "send_chat_message": {"args_contain": {"message": "nope"}},
    }}})
    assert not evaluate_spec(bad_args, _dry(trace=trace))["passed"]


def test_evaluator_args_contain_subset_semantics():
    trace = [_tool_entry("s", "t", {"to": "x@y.z", "opts": {"cc": "me", "n": 3}})]
    ok = SpecDef.model_validate({"expect": {"tool_calls": {
        "t": {"args_contain": {"opts": {"n": 3}}},
    }}})
    assert evaluate_spec(ok, _dry(trace=trace))["passed"]
    bad = SpecDef.model_validate({"expect": {"tool_calls": {
        "t": {"args_contain": {"opts": {"n": 4}}},
    }}})
    assert not evaluate_spec(bad, _dry(trace=trace))["passed"]


def test_evaluator_output_contains():
    trace = [_tool_entry("s", "t", result={"greeting": "hello from QA"})]
    ok = SpecDef.model_validate({"expect": {"output_contains": {"s": "from QA"}}})
    assert evaluate_spec(ok, _dry(trace=trace))["passed"]
    never_ran = SpecDef.model_validate({"expect": {"output_contains": {"zz": "x"}}})
    res = evaluate_spec(never_ran, _dry(trace=trace))
    assert "never ran" in res["failures"][0]


# ---- tools -----------------------------------------------------------------

SPEC_OK = (
    "description: greeting mentions the name\n"
    "inputs: {greeting: 'hi Roy'}\n"
    "expect:\n"
    "  status: done\n"
    "  tool_calls:\n"
    "    send_chat_message: {count: 1, args_contain: {message: 'Roy'}}\n"
)

SPEC_FAILING = (
    "inputs: {greeting: 'hi Roy'}\n"
    "expect:\n"
    "  tool_calls:\n"
    "    send_chat_message: {args_contain: {message: 'Slartibartfast'}}\n"
)


@pytest.mark.asyncio
async def test_spec_add_runs_immediately_and_upserts(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await handlers["playbook_spec_add"](
        name="greeter", spec_name="mentions-name", spec_yaml=SPEC_OK,
    ))
    assert out["status"] == "created"
    assert out["result"]["passed"] is True
    assert out["ran_against_version"] == 1

    out2 = json.loads(await handlers["playbook_spec_add"](
        name="greeter", spec_name="mentions-name", spec_yaml=SPEC_FAILING,
    ))
    assert out2["status"] == "updated"
    assert out2["result"]["passed"] is False
    assert "warning" in out2
    async with sf() as s:
        rows = (await s.execute(select(PlaybookSpec))).scalars().all()
    assert len(rows) == 1  # upsert, not a duplicate
    assert rows[0].last_result["passed"] is False


@pytest.mark.asyncio
async def test_spec_add_rejects_invalid_yaml(env):
    _, _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await handlers["playbook_spec_add"](
        name="greeter", spec_name="bad", spec_yaml="expect: {status: maybe}",
    ))
    assert "error" in out


@pytest.mark.asyncio
async def test_spec_list_and_delete(env):
    _, _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    empty = json.loads(await handlers["playbook_spec_list"](name="greeter"))
    assert empty["count"] == 0
    assert "note" in empty
    await handlers["playbook_spec_add"](
        name="greeter", spec_name="s1", spec_yaml=SPEC_OK,
    )
    lst = json.loads(await handlers["playbook_spec_list"](name="greeter"))
    assert lst["count"] == 1
    assert lst["specs"][0]["last_result"]["passed"] is True
    gone = json.loads(await handlers["playbook_spec_delete"](
        name="greeter", spec_name="s1",
    ))
    assert gone["status"] == "deleted"
    assert json.loads(
        await handlers["playbook_spec_list"](name="greeter")
    )["count"] == 0
    missing = json.loads(await handlers["playbook_spec_delete"](
        name="greeter", spec_name="s1",
    ))
    assert "error" in missing


@pytest.mark.asyncio
async def test_spec_run_targets_candidate_by_default(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](
        name="greeter", spec_name="s1", spec_yaml=SPEC_OK,
    )
    # candidate replaces the greeting input with a constant WITHOUT 'Roy'
    new_code = CODE.replace("inputs.greeting", "'plain hello'")
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers, "greeter"), code=new_code,
    )
    out = json.loads(await handlers["playbook_spec_run"](name="greeter"))
    assert out["is_candidate"] is True
    assert out["failed"] == 1  # candidate broke the expectation
    live = json.loads(await handlers["playbook_spec_run"](
        name="greeter", version="live",
    ))
    assert live["is_candidate"] is False
    assert live["failed"] == 0  # live still passes


@pytest.mark.asyncio
async def test_candidate_save_autoruns_specs(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](
        name="greeter", spec_name="s1", spec_yaml=SPEC_OK,
    )
    new_code = CODE.replace("inputs.greeting", "'plain hello'")
    out = json.loads(await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers, "greeter"), code=new_code,
    ))
    assert out["status"] == "candidate_saved"  # save NOT blocked
    assert out["specs"]["failed"] == 1
    assert out["specs"]["failures"][0]["spec"] == "s1"
    assert "publish will refuse" in out["next"].lower()


@pytest.mark.asyncio
async def test_promote_refused_on_failing_spec_then_passes_after_fix(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_spec_add"](
        name="greeter", spec_name="s1", spec_yaml=SPEC_OK,
    )
    new_code = CODE.replace("inputs.greeting", "'plain hello'")
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers, "greeter"), code=new_code,
    )
    refused = json.loads(await handlers["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(handlers)))
    assert refused["gate"] == "specs"
    assert refused["failing_specs"][0]["spec"] == "s1"
    pb = await _get(sf, "greeter")
    assert pb.live_version == 1  # promote did not happen
    assert pb.candidate_version == 2  # candidate intact

    # fix the candidate so the spec passes, then promote succeeds
    good_code = CODE.replace("inputs.greeting", "'hello dear Roy'")
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers, "greeter"), code=good_code,
    )
    await green_run(sf, 3)
    out = json.loads(await handlers["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(handlers)))
    assert out["status"] == "published"
    gates = {g["gate"]: g for g in out["gates"]}
    assert gates["specs"]["ok"] is True
    assert "1/1" in gates["specs"]["note"]


@pytest.mark.asyncio
async def test_promote_with_no_specs_passes_with_note(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    await handlers["playbook_edit"](
        name="greeter", ticket=await _ticket(handlers, "greeter"),
        old="says hi", new="says hello",
    )
    await green_run(sf, 2)
    out = json.loads(await handlers["playbook_publish"](explanation=EXPLANATION, name="greeter", plan_id=await make_plan(handlers)))
    assert out["status"] == "published"
    gates = {g["gate"]: g for g in out["gates"]}
    assert gates["specs"]["ok"] is True
    assert gates["specs"]["note"] == "no specs defined"


@pytest.mark.asyncio
async def test_spec_from_run_builds_proposal(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](
        name="greeter", code=CODE, agent_autonomy="agent_may_trigger",
    )
    out = json.loads(await handlers["playbook_run"](
        name="greeter", inputs=json.dumps({"greeting": "hi Roy"}),
    ))
    assert out.get("status") == "done", out
    pin = json.loads(await handlers["playbook_spec_from_run"](name="greeter"))
    assert pin["run_id"] == out["run_id"]
    doc = parse_spec_yaml(pin["spec_yaml"])
    assert doc.inputs == {"greeting": "hi Roy"}
    assert doc.expect.status == "done"
    assert doc.expect.steps_ran == ["say"]
    assert doc.expect.tool_calls["send_chat_message"].count == 1
    assert doc.stubs["say"] == {"ok": True}  # recorded tool result
    # the proposal round-trips: saving it as a spec passes against live
    saved = json.loads(await handlers["playbook_spec_add"](
        name="greeter", spec_name="pinned", spec_yaml=pin["spec_yaml"],
    ))
    assert saved["result"]["passed"] is True


@pytest.mark.asyncio
async def test_spec_from_run_no_runs(env):
    _, _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await handlers["playbook_spec_from_run"](name="greeter"))
    assert "error" in out


# 012 phase 4 — real shapes first: failed runs are pinnable too.

FAIL_CODE = (
    "playbook(name='crasher', description='fails on step b')\n"
    "a = tool('t')\n"
    "b = tool('send_chat_message', message='{{ steps.a.result.nope }}')\n"
)


@pytest.mark.asyncio
async def test_spec_from_run_failed_run_pins_failure(env):
    sf, runner, handlers, _ = env
    await handlers["playbook_propose"](
        name="crasher", code=FAIL_CODE, agent_autonomy="agent_may_trigger",
    )
    out = json.loads(await handlers["playbook_run"](name="crasher"))
    assert out.get("status") == "failed", out
    # auto-pick with no done run falls back to the latest failed run
    pin = json.loads(await handlers["playbook_spec_from_run"](name="crasher"))
    assert pin["run_id"] == out["run_id"]
    assert "FAILED run" in pin["next"]
    doc = parse_spec_yaml(pin["spec_yaml"])
    assert doc.expect.status == "failed"
    # step a DID run — its real output is pinned; failing step b is not
    assert doc.stubs["a"] == {"ok": True}
    assert "b" not in doc.stubs
    # the failure point is pinned as a substring expectation
    assert doc.expect.error_contains
    status = json.loads(await handlers["playbook_status"](run_id=out["run_id"]))
    assert doc.expect.error_contains in status["error"]
    # the proposal round-trips: saved as-is it documents current behavior
    saved = json.loads(await handlers["playbook_spec_add"](
        name="crasher", spec_name="pinned-failure", spec_yaml=pin["spec_yaml"],
    ))
    assert saved["result"]["passed"] is True, saved


@pytest.mark.asyncio
async def test_spec_from_run_prefers_done_over_newer_failed(env):
    sf, _, handlers, _ = env
    code = (
        "playbook(name='flaky', description='d')\n"
        "say = tool('send_chat_message', message='{{ inputs.x.y }}')\n"
    )
    await handlers["playbook_propose"](
        name="flaky", code=code, agent_autonomy="agent_may_trigger",
    )
    good = json.loads(await handlers["playbook_run"](
        name="flaky", inputs=json.dumps({"x": {"y": "hi"}}),
    ))
    assert good["status"] == "done", good
    bad = json.loads(await handlers["playbook_run"](
        name="flaky", inputs=json.dumps({"x": {}}),
    ))
    assert bad["status"] == "failed", bad
    pin = json.loads(await handlers["playbook_spec_from_run"](name="flaky"))
    assert pin["run_id"] == good["run_id"]  # done wins over newer failed
    # the failed run is still reachable explicitly
    pin2 = json.loads(await handlers["playbook_spec_from_run"](
        name="flaky", run_id=bad["run_id"],
    ))
    assert parse_spec_yaml(pin2["spec_yaml"]).expect.status == "failed"


@pytest.mark.asyncio
async def test_spec_from_run_rejects_unfinished_run(env):
    sf, _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    pb = await _get(sf, "greeter")
    async with sf() as s:
        run = PlaybookRun(playbook_id=pb.id, playbook_version=1, status="running")
        s.add(run)
        await s.commit()
        run_id = str(run.id)
    out = json.loads(await handlers["playbook_spec_from_run"](
        name="greeter", run_id=run_id,
    ))
    assert "still 'running'" in out["error"]


@pytest.mark.asyncio
async def test_status_failed_run_hints_spec_from_run(env):
    _, _, handlers, _ = env
    await handlers["playbook_propose"](
        name="crasher", code=FAIL_CODE, agent_autonomy="agent_may_trigger",
    )
    out = json.loads(await handlers["playbook_run"](name="crasher"))
    assert out["status"] == "failed", out
    status = json.loads(await handlers["playbook_status"](run_id=out["run_id"]))
    hint = status.get("hint", "")
    assert "playbook_spec_from_run" in hint
    assert "crasher" in hint and out["run_id"] in hint


@pytest.mark.asyncio
async def test_spec_tool_policies(env):
    _, _, _, tools = env
    assert tools["playbook_spec_delete"][0].policy == "prompt_always"
    for name in ("playbook_spec_add", "playbook_spec_list",
                 "playbook_spec_run", "playbook_spec_from_run"):
        assert getattr(tools[name][0], "policy", None) != "prompt_always"


def test_spec_tools_are_skill_gated():
    from plugin_playbooks import PlaybooksPlugin
    skill_tools = set(PlaybooksPlugin.manifest.skills[0].tools)
    gated = set(PlaybooksPlugin.AUTHORING_TOOLS)
    for name in ("playbook_spec_add", "playbook_spec_list", "playbook_spec_delete",
                 "playbook_spec_run", "playbook_spec_from_run"):
        assert name in skill_tools
        assert name in gated
    # phase-3 rule: every SkillDef tool must be in AUTHORING_TOOLS
    assert skill_tools <= gated


# ---- 0.14.2: unmatched stubs fail loudly; undefined refs name real keys ----

@pytest.mark.asyncio
async def test_spec_with_unmatched_stub_key_fails(env):
    sf, runner, handlers, _ = env
    from plugin_playbooks.specs import run_spec
    pb = Playbook(
        name="p2", display_name="p2", status="enabled",
        definition={"name": "p2", "steps": [
            {"id": "fetch", "kind": "tool_call", "tool": "t", "args": {}},
        ]},
    )
    spec = SpecDef(stubs={"fetch_sourcez": {"rows": []}})
    res = await run_spec(runner, pb, spec)
    assert res["passed"] is False
    assert any("matches no step id or tool name" in f for f in res["failures"])
    assert any("fetch" in f and "t" in f for f in res["failures"])

    # a correctly keyed stub does not trigger the check
    ok = await run_spec(runner, pb, SpecDef(stubs={"fetch": {"rows": []}}))
    assert not any("matches no step id" in f for f in ok["failures"])


@pytest.mark.asyncio
async def test_undefined_ref_error_names_real_keys(env):
    sf, runner, handlers, _ = env
    # loop over a path that doesn't exist in the (unstubbed) dry output:
    # the error must name the real keys at the failing segment, not blame
    # a "schemaless llm_step".
    pb = Playbook(
        name="p3", display_name="p3", status="enabled",
        definition={"name": "p3", "steps": [
            {"id": "fetch", "kind": "tool_call", "tool": "t", "args": {}},
            {"id": "crawl", "kind": "loop",
             "over": "steps.fetch[\"result\"][\"rows\"]",
             "body": [
                 {"id": "inner", "kind": "tool_call", "tool": "t", "args": {}},
             ]},
        ]},
    )
    out = await runner.dry_run(pb)
    assert out["status"] == "failed"
    assert "steps.fetch.result.rows does not exist" in out["error"]
    assert "_dry" in out["error"]  # the real key present at that level
    assert "schemaless llm_step" not in out["error"]


# ---- batch spec_add (plans/012 phase 1) ------------------------------------

BATCH_OK = (
    "mentions-name:\n"
    "  description: greeting mentions the name\n"
    "  inputs: {greeting: 'hi Roy'}\n"
    "  expect:\n"
    "    status: done\n"
    "    tool_calls:\n"
    "      send_chat_message: {count: 1, args_contain: {message: 'Roy'}}\n"
    "wrong-name:\n"
    "  inputs: {greeting: 'hi Roy'}\n"
    "  expect:\n"
    "    tool_calls:\n"
    "      send_chat_message: {args_contain: {message: 'Slartibartfast'}}\n"
)


@pytest.mark.asyncio
async def test_spec_add_batch_upserts_and_runs_suite_once(env):
    sf, _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await handlers["playbook_spec_add"](
        name="greeter", specs=BATCH_OK,
    ))
    assert out["specs"] == {"mentions-name": "created", "wrong-name": "created"}
    assert out["total"] == 2
    assert out["passed"] == 1
    assert out["failed"] == 1
    assert "warning" in out  # a failing spec was stored → promote gate warning
    async with sf() as s:
        rows = (await s.execute(select(PlaybookSpec))).scalars().all()
    assert len(rows) == 2

    # second batch touching one existing spec → updated, still 2 rows
    out2 = json.loads(await handlers["playbook_spec_add"](
        name="greeter",
        specs=(
            "wrong-name:\n"
            "  inputs: {greeting: 'hi Roy'}\n"
            "  expect:\n"
            "    tool_calls:\n"
            "      send_chat_message: {args_contain: {message: 'Roy'}}\n"
        ),
    ))
    assert out2["specs"] == {"wrong-name": "updated"}
    assert out2["failed"] == 0
    assert "warning" not in out2
    async with sf() as s:
        rows = (await s.execute(select(PlaybookSpec))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_spec_add_batch_partial_parse_failure_keeps_good_specs(env):
    sf, _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    batch = (
        "good:\n"
        "  inputs: {greeting: 'hi Roy'}\n"
        "  expect: {status: done}\n"
        "bad:\n"
        "  expect: {status: maybe}\n"
    )
    out = json.loads(await handlers["playbook_spec_add"](name="greeter", specs=batch))
    assert out["specs"] == {"good": "created"}
    assert "bad" in out["spec_errors"]
    assert "note" in out
    async with sf() as s:
        rows = (await s.execute(select(PlaybookSpec))).scalars().all()
    assert [r.name for r in rows] == ["good"]


@pytest.mark.asyncio
async def test_spec_add_batch_all_bad_stores_nothing(env):
    sf, _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    out = json.loads(await handlers["playbook_spec_add"](
        name="greeter", specs="bad: {expect: {status: maybe}}",
    ))
    assert "error" in out
    assert "bad" in out["spec_errors"]
    async with sf() as s:
        rows = (await s.execute(select(PlaybookSpec))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_spec_add_form_validation(env):
    _, _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    both = json.loads(await handlers["playbook_spec_add"](
        name="greeter", spec_name="x", spec_yaml=SPEC_OK, specs=BATCH_OK,
    ))
    assert "not both" in both["error"]
    neither = json.loads(await handlers["playbook_spec_add"](name="greeter"))
    assert "specs=" in neither["error"]
    half = json.loads(await handlers["playbook_spec_add"](
        name="greeter", spec_name="x",
    ))
    assert "both spec_name and spec_yaml" in half["error"]
    not_map = json.loads(await handlers["playbook_spec_add"](
        name="greeter", specs="- a\n- b\n",
    ))
    assert "mapping" in not_map["error"]
