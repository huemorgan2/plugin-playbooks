"""plans/032 phase 12 — the migration comparison helper
(`plugin_playbooks/v2/migrate.py`) and `playbook_dry_run(compare=true)`.

Tests 1-6 are pure (no DB, no jail). Test 7 drives the v2 dry run through
the segment loop on the scripted `code_run` fake of `tests/test_v2_loop.py`
(the harness of `tests/test_v2_end_to_end.py`, file-backed sqlite: a real
`playbook_run` task seeds the green live run's step rows).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from evidence import EXPLANATION, green_run
from readstage import parse_read_stage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_v2_loop import _Tool, _Tools
from v2harness import Bus, Env, ScriptedCodeRun, _Approvals, _Ctx, _effect

from plugin_playbooks import _ensure_columns
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.v2.migrate import (
    Effect,
    compare_effects,
    require_green_live_run,
    v1_effects,
    v2_effects,
    v2_groups,
)

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _row(step_id: str, kind: str, n: int, inputs=None, outputs=None, status="done") -> PlaybookStepRun:
    return PlaybookStepRun(
        id=uuid.uuid4(), run_id=uuid.uuid4(), step_id=step_id, step_kind=kind,
        status=status, inputs=inputs, outputs=outputs, started_at=T0 + timedelta(seconds=n),
    )


ALL_KINDS_DEFINITION = {
    "name": "kinds",
    "steps": [
        {"id": "loop", "kind": "loop", "over": "[1, 2]", "body": [
            {"id": "s1", "kind": "tool_call", "tool": "fetch", "args": {"q": "{{ item }}"}},
        ]},
        {"id": "st", "kind": "state", "ops": []},
        {"id": "c", "kind": "code", "code": "return 1"},
        {"id": "l", "kind": "llm_step", "prompt": "summarise"},
        {"id": "a", "kind": "agent_step", "prompt": "decide"},
        {"id": "cond", "kind": "condition", "when": "true", "then": [
            {"id": "w", "kind": "wait_for_approval"},
        ]},
        {"id": "e", "kind": "wait_for_event", "event": "order.paid"},
        {"id": "par", "kind": "parallel", "branches": [
            [{"id": "p1", "kind": "tool_call", "tool": "x"}],
            [{"id": "p2", "kind": "tool_call", "tool": "y"}],
        ]},
        {"id": "sub", "kind": "subtask", "playbook": "child", "inputs_map": {"k": "1"}},
        {"id": "h", "kind": "halt"},
    ],
}


# ------------------------------------------------------------------ 1
def test_v1_effects_maps_every_step_kind():
    rows = [
        _row("loop", "loop", 0, outputs={"iterations": 2}),
        _row("s1", "tool_call", 1, inputs={"q": 1}, outputs={"tool": "fetch", "result": {"r": 1}}),
        _row("s1", "tool_call", 2, inputs={"q": 2}, outputs={"tool": "fetch", "result": {"r": 2}}),
        _row("st", "state", 3, inputs={"ops": []}),
        _row("c", "code", 4, inputs={"inputs": {}}, outputs={"result": 1}),
        _row("l", "llm_step", 5, inputs={"prompt": "summarise"}, outputs={"_raw": "s"}),
        _row("a", "agent_step", 6, inputs={"prompt": "decide"}, outputs={"_raw": "d"}),
        _row("cond", "condition", 7, outputs={"branch": "then", "condition": True}),
        _row("w", "wait_for_approval", 8, outputs={"approved": True, "auto": True}),
        _row("e", "wait_for_event", 9, outputs={"event": "order.paid", "received": False, "stub": True}),
        _row("par", "parallel", 10, outputs={"branches": []}),
        _row("p1", "tool_call", 11, inputs={}, outputs={"tool": "x", "result": None}),
        _row("p2", "tool_call", 11, inputs={}, outputs={"tool": "y", "result": None}),
        _row("sub", "subtask", 12, inputs={"k": "1"}, outputs={"subtask_run_id": "r", "status": "done"}),
        _row("h", "halt", 13, outputs={"halted": True}),
    ]
    # twelve StepKind values covered by the rows
    assert {r.step_kind for r in rows} == {
        "tool_call", "agent_step", "llm_step", "condition", "parallel", "wait_for_approval",
        "wait_for_event", "subtask", "loop", "state", "halt", "code",
    }
    # rows handed over shuffled: the helper orders by started_at then id
    effects = v1_effects(ALL_KINDS_DEFINITION, list(reversed(rows)))
    assert [e.kind for e in effects] == [
        "tool", "tool", "llm", "agent", "approve", "wait_event", "tool", "tool", "subtask",
    ]
    assert set(e.kind for e in effects) == {"tool", "llm", "agent", "approve", "wait_event", "subtask"}
    by_occ = {e.occurrence: e for e in effects}
    assert by_occ["s1#1"].args == {"q": 1} and by_occ["s1#2"].args == {"q": 2}
    assert by_occ["s1#1"].name == "fetch"
    assert by_occ["l#1"].args == {"prompt": "summarise"} and by_occ["l#1"].name is None
    assert by_occ["w#1"].kind == "approve" and by_occ["w#1"].args == {}
    assert by_occ["e#1"].name == "order.paid"
    assert by_occ["sub#1"].name == "child" and by_occ["sub#1"].args == {"inputs": {"k": "1"}}
    # parallel children share the container's group; nothing else is grouped
    assert by_occ["p1#1"].group == "par" == by_occ["p2#1"].group
    assert all(e.group is None for e in effects if e.occurrence not in ("p1#1", "p2#1"))


# ------------------------------------------------------------------ 2
def _base() -> list[Effect]:
    return [
        Effect("tool", "fetch", {"q": 1}, "a#1"),
        Effect("llm", None, {"prompt": "p"}, "b#1"),
        Effect("tool", "send", {"to": "a"}, "c#1"),
    ]


def test_compare_match_on_identical_sequences():
    out = compare_effects(_base(), _base())
    assert out["match"] is True and out["mismatches"] == []
    assert (out["v1_count"], out["v2_count"]) == (3, 3)


# ------------------------------------------------------------------ 3
def _variant(cls: str) -> tuple[list[Effect], int]:
    v2 = _base()
    if cls == "order":
        v2 = [v2[1], v2[0], v2[2]]
        return v2, 0
    if cls == "missing":
        return v2[:2], 2
    if cls == "extra":
        return v2 + [Effect("tool", "log", {}, "d#1")], 3
    if cls == "kind":
        v2[1] = Effect("agent", None, {"prompt": "p"}, "b#1")
        return v2, 1
    if cls == "name":
        v2[2] = Effect("tool", "mail", {"to": "a"}, "c#1")
        return v2, 2
    if cls == "args":
        v2[2] = Effect("tool", "send", {"to": "b"}, "c#1")
        return v2, 2
    raise AssertionError(cls)


@pytest.mark.parametrize("cls", ["order", "missing", "extra", "kind", "name", "args"])
def test_compare_reports_each_mismatch_class(cls):
    v2, position = _variant(cls)
    out = compare_effects(_base(), v2)
    assert out["match"] is False
    assert len(out["mismatches"]) == 1, out["mismatches"]
    finding = out["mismatches"][0]
    assert finding["class"] == cls and finding["position"] == position
    if cls == "args":
        assert finding["paths"] == [{"path": "/to", "v1": "a", "v2": "b"}]
        assert finding["v1"]["occurrence"] == "c#1" and finding["v2"]["args"] == {"to": "b"}
    if cls == "missing":
        assert finding["v2"] is None and finding["v1"]["occurrence"] == "c#1"
    if cls == "extra":
        assert finding["v1"] is None and finding["v2"]["occurrence"] == "d#1"
    if cls == "order":
        assert finding["v1"] == ["a#1", "b#1", "c#1"] and finding["v2"] == ["b#1", "a#1", "c#1"]


def test_compare_groups_are_multisets():
    # a v1 parallel pair vs a v2 gather pair in the other order: a match
    v1 = [Effect("tool", "x", {}, "p1#1", "par"), Effect("tool", "y", {}, "p2#1", "par")]
    v2 = [Effect("tool", "y", {}, "p2#1", "gather-p1"), Effect("tool", "x", {}, "p1#1", "gather-p1")]
    assert compare_effects(v1, v2)["match"] is True
    # ungrouped, the same pair is an `order` finding
    assert compare_effects(
        [Effect("tool", "x", {}, "p1#1"), Effect("tool", "y", {}, "p2#1")],
        [Effect("tool", "y", {}, "p2#1"), Effect("tool", "x", {}, "p1#1")],
    )["mismatches"][0]["class"] == "order"
    # v2_groups reads gather membership from the code
    code = (
        "async def run(ctx, inputs):\n"
        "    a, b = await ctx.gather(ctx.tool('x', _id='p1'), ctx.tool('y', _id='p2'))\n"
        "    return await ctx.tool('z', _id='c')\n"
    )
    groups = v2_groups(code)
    assert groups == {"p1": "gather-p1", "p2": "gather-p1"}
    # v2_effects: entry 0 and now/random/log skipped, names per kind, groups applied
    journal = [
        {"seq": 0, "kind": "run", "inputs": {}},
        {"seq": 1, "kind": "tool", "id": "p1", "occurrence": 1, "name": "x", "args": {}},
        {"seq": 2, "kind": "tool", "id": "p2", "occurrence": 1, "name": "y", "args": {}},
        {"seq": 3, "kind": "now", "id": "now", "occurrence": 1, "name": None, "args": {}},
        {"seq": 4, "kind": "log", "id": "log", "occurrence": 1, "name": None, "args": {"message": "m"}},
        {"seq": 5, "kind": "wait_event", "id": "w", "occurrence": 1, "name": None,
         "args": {"name": "order.paid", "filter": {}, "timeout": 60}},
        {"seq": 6, "kind": "subtask", "id": "s", "occurrence": 1, "name": "child",
         "args": {"inputs": {"k": 1}, "returns": None}},
        {"seq": 7, "kind": "tool", "id": "c", "occurrence": 2, "name": "z", "args": {"q": 1}},
    ]
    effects = v2_effects(journal, groups)
    assert [(e.kind, e.name, e.occurrence, e.group) for e in effects] == [
        ("tool", "x", "p1#1", "gather-p1"), ("tool", "y", "p2#1", "gather-p1"),
        ("wait_event", "order.paid", "w#1", None), ("subtask", "child", "s#1", None),
        ("tool", "z", "c#2", None),
    ]
    assert effects[2].args == {} and effects[3].args == {"inputs": {"k": 1}}


# ------------------------------------------------------------------ 4
def test_compare_keeps_vault_refs_raw_and_drops_effect_options():
    v1 = [Effect("tool", "http", {"headers": {"x-api-key": "vault:acme"}, "_id": "s1"}, "s1#1")]
    v2 = [Effect("tool", "http", {"headers": {"x-api-key": "vault:acme"}}, "s1#1")]
    out = compare_effects(v1, v2)
    assert out["match"] is True and out["mismatches"] == []
    text = json.dumps(out)
    assert "vault:acme" in text and "_id" not in text
    keys = [e["args"]["headers"]["x-api-key"] for side in ("v1", "v2") for e in out["effects"][side]]
    assert keys == ["vault:acme", "vault:acme"]


def test_compare_accepts_v1_rendered_strings():
    # v1 renders `{{ steps.fetch.result.rows }}` to str(list); v2 passes the list
    rows = [{"id": 1, "q": "x"}]
    v1 = [Effect("tool", "send", {"to": "a@x", "body": str(rows)}, "send#1")]
    v2 = [Effect("tool", "send", {"to": "a@x", "body": rows}, "send#1")]
    assert compare_effects(v1, v2)["match"] is True
    # a different list is still an `args` finding on /body
    other = [Effect("tool", "send", {"to": "a@x", "body": rows + [{"id": 2}]}, "send#1")]
    out = compare_effects(v1, other)
    assert out["mismatches"][0]["class"] == "args"
    assert [p["path"] for p in out["mismatches"][0]["paths"]] == ["/body"]


# ------------------------------------------------------------------ 5
def test_compare_llm_prompt_prefix_only():
    prompt = "x" * 2000
    v1 = [Effect("llm", None, {"prompt": prompt}, "l#1")]
    same = [Effect("llm", None, {"prompt": prompt + " and more beyond the cut"}, "l#1")]
    assert compare_effects(v1, same)["match"] is True
    differ = [Effect("llm", None, {"prompt": "y" + prompt[1:]}, "l#1")]
    out = compare_effects(v1, differ)
    assert out["match"] is False and out["mismatches"][0]["class"] == "args"
    assert [p["path"] for p in out["mismatches"][0]["paths"]] == ["/prompt"]
    # an llm/agent effect compares nothing but the prompt
    extra = [Effect("llm", None, {"prompt": prompt, "purpose": "x", "_timeout": 5}, "l#1")]
    assert compare_effects(v1, extra)["match"] is True


# ------------------------------------------------------------------ 6
@pytest.mark.parametrize("status,is_test,version,live,ok", [
    ("failed", False, 3, 3, False),
    ("done", True, 3, 3, False),
    ("done", False, 2, 3, False),
    ("done", False, 3, None, False),
    ("done", False, 3, 3, True),
])
def test_require_green_live_run_refuses_non_green(status, is_test, version, live, ok):
    if ok:
        require_green_live_run(status, is_test, version, live)
        return
    with pytest.raises(ValueError) as info:
        require_green_live_run(status, is_test, version, live)
    assert "last green live run" in str(info.value)
    assert "status 'done', not a test run, of the live version" in str(info.value)


# ------------------------------------------------------------------ 7
PBLANG = (
    "playbook(name='mig', description='fetch then send')\n"
    "fetch = tool('fetch', q=inputs.q)\n"
    "send = tool('send', to='a@x', body=fetch.result.rows)\n"
)


def _python(to: str) -> str:
    return (
        "async def run(ctx, inputs):\n"
        "    fetch = await ctx.tool('fetch', q=inputs['q'], _id='fetch')\n"
        f"    send = await ctx.tool('send', to='{to}', body=fetch['rows'], _id='send')\n"
        "    return send\n"
    )


async def _fetch(**kw):
    return {"rows": [{"id": 1, "q": kw.get("q")}]}


async def _send(**kw):
    return {"sent": True}


async def _env(tmp_path, state: dict) -> Env:
    """What the real shim would do for `_python(state['to'])`: fetch, then
    send with the recorded fetch result, then return."""

    def script(envelope: dict) -> dict:
        journal = envelope["journal"]
        inputs = journal[0]["inputs"]
        if len(journal) == 1:
            return _effect(1, "fetch", 1, "tool", "fetch", {"q": inputs["q"]})
        if len(journal) == 2:
            return _effect(2, "send", 1, "tool", "send",
                           {"to": state["to"], "body": journal[1]["result"]["rows"]})
        return {"kind": "return", "value": journal[2]["result"]}

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/migration.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_columns(engine)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    registry = _Tools(fetch=_Tool(_fetch), send=_Tool(_send))
    code_run = ScriptedCodeRun(script)
    registry.add("code_run", code_run.handler)
    bus = Bus()
    runner = PlaybookRunner(session_factory=sf, tool_registry=registry, events=bus)
    approvals = _Approvals(decision="approved")
    ctx = _Ctx(approvals)
    pairs = build_tools(sf, bus, runner, ctx)
    tools = {td.name: h for td, h in pairs}
    defs = {td.name: td for td, _ in pairs}
    return Env(engine, sf, tools, defs, bus, runner, code_run, approvals, ctx, registry)


async def _run_count(sf) -> int:
    async with sf() as s:
        return (await s.execute(select(func.count()).select_from(PlaybookRun))).scalar_one()


async def _test_run_row(sf, version: int) -> str:
    async with sf() as s:
        pb = (await s.execute(select(Playbook).where(Playbook.name == "mig"))).scalar_one()
        row = PlaybookRun(
            playbook_id=pb.id, playbook_version=version, status="done",
            trigger="agent-candidate", is_test=True, started_at=T0, completed_at=T0,
        )
        s.add(row)
        await s.commit()
        return str(row.id)


async def _edit(e: Env, code: str) -> dict:
    read = parse_read_stage(await e.tools["playbook_edit"](name="mig"))
    out = json.loads(await e.tools["playbook_edit"](name="mig", ticket=read["ticket"], code=code))
    assert out["status"] == "candidate_saved", out
    return out


async def test_dry_run_compare_option_end_to_end(tmp_path):
    state = {"to": "a@x"}
    e = await _env(tmp_path, state)
    try:
        # pblang v1 published live, then one REAL live run (the rows the
        # comparison reads: fetch#1 / send#1 with the rendered args)
        out = json.loads(await e.tools["playbook_propose"](
            name="mig", code=PBLANG, agent_autonomy="agent_may_trigger",
        ))
        assert out["status"] == "candidate_saved", out
        await green_run(e.sf, 1)
        out = json.loads(await e.tools["playbook_publish"](name="mig", explanation=EXPLANATION))
        assert out["status"] == "published", out
        live = json.loads(await e.tools["playbook_run"](
            name="mig", inputs='{"q": "x"}', wait_seconds=10,
        ))
        assert live["status"] == "done" and live["kind"] == "real_run", live
        assert e.code_run.calls == []
        run_id = live["run_id"]
        # the python candidate reuses the v1 step ids as `_id`
        out = await _edit(e, _python("a@x"))
        assert out["format"] == "python" and out["candidate_version"] == 2
        before = await _run_count(e.sf)
        assert dict(e.defs["playbook_dry_run"].parameters["properties"]["compare"])["type"] == "boolean"

        dry = json.loads(await e.tools["playbook_dry_run"](
            name="mig", version="candidate", inputs='{"q": "x"}',
            stubs_from_run=run_id, compare=True,
        ))
        assert dry["status"] == "simulated" and dry["kind"] == "dry_run", dry
        assert dry["compared_run_id"] == run_id and dry["compared_run_version"] == 1
        assert dry["comparison"]["match"] is True, dry["comparison"]
        assert dry["comparison"]["mismatches"] == []
        assert (dry["comparison"]["v1_count"], dry["comparison"]["v2_count"]) == (2, 2)
        assert set(dry["steps_ran"]) == {"fetch#1", "send#1"}
        assert set(dry["steps_ran"]) == set(dry["stubs_source"]["occurrences_used"])
        assert dry["stubs_source"]["occurrences_unmatched"] == []
        assert dry["stubs_source"]["format"] == "pblang"
        assert dry["unreached_call_sites"] == []
        assert await _run_count(e.sf) == before

        # a candidate with a changed tool arg: `args` finding on /to
        state["to"] = "b@x"
        out = await _edit(e, _python("b@x"))
        assert out["candidate_version"] == 3
        dry = json.loads(await e.tools["playbook_dry_run"](
            name="mig", version="candidate", inputs='{"q": "x"}',
            stubs_from_run=run_id, compare="true",
        ))
        assert dry["status"] == "simulated", dry
        assert dry["comparison"]["match"] is False
        first = dry["comparison"]["mismatches"][0]
        assert first["class"] == "args" and first["position"] == 1
        assert first["paths"] == [{"path": "/to", "v1": "a@x", "v2": "b@x"}]
        assert await _run_count(e.sf) == before

        # compare without stubs_from_run: refused, nothing simulated
        out = json.loads(await e.tools["playbook_dry_run"](name="mig", version="candidate", compare=True))
        assert "stubs_from_run" in out["error"] and "steps_ran" not in out
        assert await _run_count(e.sf) == before

        # compare against a test run: refused, naming the rule
        test_id = await _test_run_row(e.sf, 1)
        after_seed = await _run_count(e.sf)
        out = json.loads(await e.tools["playbook_dry_run"](
            name="mig", version="candidate", inputs='{"q": "x"}',
            stubs_from_run=test_id, compare=True,
        ))
        assert "last green live run" in out["error"] and "test run" in out["error"], out
        assert out["compared_run_id"] == test_id and "steps_ran" not in out
        assert await _run_count(e.sf) == after_seed
        # and the plain (compare=false) dry run is unchanged by the option
        plain = json.loads(await e.tools["playbook_dry_run"](
            name="mig", version="candidate", inputs='{"q": "x"}', stubs_from_run=run_id,
        ))
        assert plain["status"] == "simulated" and "comparison" not in plain
        assert await _run_count(e.sf) == after_seed
    finally:
        await e.dispose()
