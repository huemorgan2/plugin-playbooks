"""plans/026 — navigable dry stubs.

An unstubbed tool/code step's dry result must be navigable: any
`steps.<id>.result.<field>` chain resolves to a visibly-fake `<dry:...>`
placeholder instead of raising StrictUndefined UndefinedError. Loops over
such values iterate zero times. Stubs keep winning verbatim.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugin_playbooks.runner import PlaybookRunner


def _bare_runner(tools: dict) -> PlaybookRunner:
    class _Reg:
        def get(self, name):
            if name not in tools:
                raise KeyError(name)
            return tools[name]

    r = PlaybookRunner.__new__(PlaybookRunner)
    r._tools = _Reg()
    return r


def _pb(steps: list[dict]):
    return SimpleNamespace(
        name="p",
        definition={"name": "p", "steps": steps},
        inputs_schema=None,
    )


@pytest.mark.asyncio
async def test_unstubbed_field_ref_does_not_fail_dry_run():
    runner = _bare_runner({"t": object()})
    pb = _pb([
        {"id": "a", "kind": "tool_call", "tool": "t", "args": {}},
        {"id": "b", "kind": "tool_call", "tool": "t",
         "args": {"x": "{{ steps.a.result.some_field }}"}},
    ])
    out = await runner.dry_run(pb)
    assert out["status"] == "done", out["error"]
    assert out["references"]["b"]["resolved_args"]["x"] == "<dry:t.some_field>"
    assert out["references"]["a"]["_dry"] is True


@pytest.mark.asyncio
async def test_chained_access_renders_dry_path():
    runner = _bare_runner({"t": object()})
    pb = _pb([
        {"id": "a", "kind": "tool_call", "tool": "t", "args": {}},
        {"id": "b", "kind": "tool_call", "tool": "t",
         "args": {"who": "{{ steps.a.result.user.email }}"}},
    ])
    out = await runner.dry_run(pb)
    assert out["status"] == "done", out["error"]
    assert out["references"]["b"]["resolved_args"]["who"] == "<dry:t.user.email>"


@pytest.mark.asyncio
async def test_loop_over_unstubbed_dry_value_iterates_zero_times():
    runner = _bare_runner({"t": object()})
    pb = _pb([
        {"id": "fetch", "kind": "tool_call", "tool": "t", "args": {}},
        {"id": "crawl", "kind": "loop", "over": "steps.fetch.result.rows",
         "body": [
             {"id": "inner", "kind": "tool_call", "tool": "t", "args": {}},
         ]},
    ])
    out = await runner.dry_run(pb)
    assert out["status"] == "done", out["error"]
    assert out["references"]["crawl"]["iterations"] == 0
    assert out["references"]["crawl"]["results"] == []


@pytest.mark.asyncio
async def test_stub_still_taken_verbatim():
    runner = _bare_runner({"t": object()})
    pb = _pb([
        {"id": "a", "kind": "tool_call", "tool": "t", "args": {}},
        {"id": "b", "kind": "tool_call", "tool": "t",
         "args": {"n": "{{ steps.a.result.rows | length }}"}},
    ])
    out = await runner.dry_run(pb, stubs={"a": {"rows": [1, 2, 3]}})
    assert out["status"] == "done", out["error"]
    assert out["references"]["a"]["stubbed"] is True
    assert out["references"]["a"]["result"] == {"rows": [1, 2, 3]}
    assert out["references"]["b"]["resolved_args"]["n"] == "3"


@pytest.mark.asyncio
async def test_unstubbed_result_still_self_describes():
    """plans/022 truthful evidence: never mistakable for a real result."""
    runner = _bare_runner({"t": object()})
    pb = _pb([{"id": "a", "kind": "tool_call", "tool": "t", "args": {}}])
    out = await runner.dry_run(pb)
    a = out["references"]["a"]
    assert a["result"]["_dry"] is True
    assert a["result"]["_note"] == "simulated — tool was NOT called"
    # wrapper carries a REAL marker too (the stub serializes as {})
    assert a["_dry"] is True
    assert a["_note"] == "simulated — tool was NOT called"


@pytest.mark.asyncio
async def test_unstubbed_code_step_result_is_navigable():
    runner = _bare_runner({"t": object()})
    pb = _pb([
        {"id": "c", "kind": "code", "source": "return {'phone': '052'}",
         "code_inputs": {}},
        {"id": "b", "kind": "tool_call", "tool": "t",
         "args": {"p": "{{ steps.c.result.phone }}"}},
    ])
    out = await runner.dry_run(pb)
    assert out["status"] == "done", out["error"]
    assert out["references"]["b"]["resolved_args"]["p"] == "<dry:c.phone>"
    assert out["references"]["c"]["_note"] == "simulated — code was NOT executed"


# --- runner-level stub seam (relocated from the removed specs suite, 0.47.0) --

@pytest.mark.asyncio
async def test_dry_run_stub_by_step_id_and_tool_name():
    runner = _bare_runner({"t": object(), "send_chat_message": object()})
    pb = _pb([
        {"id": "fetch", "kind": "tool_call", "tool": "t", "args": {}},
        {"id": "say", "kind": "tool_call", "tool": "send_chat_message",
         "args": {"message": "{{ steps.fetch.result.city }}"}},
    ])
    out = await runner.dry_run(pb, stubs={"fetch": {"city": "Haifa"}})
    assert out["status"] == "done", out["error"]
    fetch = out["references"]["fetch"]
    assert fetch["stubbed"] is True
    assert fetch["result"] == {"city": "Haifa"}
    # the stubbed value flowed into the downstream template
    assert out["references"]["say"]["resolved_args"] == {"message": "Haifa"}

    # tool-name key works too; step-id wins when both are present
    out2 = await runner.dry_run(pb, stubs={"t": {"city": "Oslo"}})
    assert out2["references"]["fetch"]["result"] == {"city": "Oslo"}
    out3 = await runner.dry_run(
        pb, stubs={"t": {"city": "Oslo"}, "fetch": {"city": "Rome"}},
    )
    assert out3["references"]["fetch"]["result"] == {"city": "Rome"}


@pytest.mark.asyncio
async def test_dry_run_stubs_agent_and_llm_steps():
    runner = _bare_runner({"t": object(), "send_chat_message": object()})
    pb = _pb([
        {"id": "judge", "kind": "llm_step", "prompt": "classify",
         "output_schema": {"label": "string"}},
        {"id": "act", "kind": "tool_call", "tool": "t",
         "args": {"v": "{{ steps.judge.label }}"}},
    ])
    out = await runner.dry_run(pb, stubs={"judge": {"label": "urgent"}})
    assert out["status"] == "done", out["error"]
    assert out["trace"][0]["output"] == {"label": "urgent"}
    assert out["trace"][1]["output"]["resolved_args"] == {"v": "urgent"}
    # without a stub the schema placeholder is used
    out2 = await runner.dry_run(pb)
    assert out2["trace"][0]["output"].get("label") != "urgent"


@pytest.mark.asyncio
async def test_loop_over_unstubbed_dry_output_iterates_zero_times():
    # a loop over a path inside an (unstubbed) dry output no longer fails
    # with UndefinedError — the navigable dry stub resolves the path to an
    # empty placeholder and the loop simply runs zero iterations.
    runner = _bare_runner({"t": object(), "send_chat_message": object()})
    pb = _pb([
        {"id": "fetch", "kind": "tool_call", "tool": "t", "args": {}},
        {"id": "crawl", "kind": "loop",
         "over": "steps.fetch[\"result\"][\"rows\"]",
         "body": [
             {"id": "inner", "kind": "tool_call", "tool": "t", "args": {}},
         ]},
    ])
    out = await runner.dry_run(pb)
    assert out["status"] == "done", out["error"]
    assert out["references"]["crawl"]["iterations"] == 0
    assert out["references"]["crawl"]["results"] == []


@pytest.mark.asyncio
async def test_missing_step_id_stays_loud():
    """Only UNSTUBBED DRY VALUES are forgiving — a typo'd step id is still a
    real authoring error and must fail the dry run."""
    runner = _bare_runner({"t": object()})
    pb = _pb([
        {"id": "a", "kind": "tool_call", "tool": "t", "args": {}},
        {"id": "b", "kind": "tool_call", "tool": "t",
         "args": {"x": "{{ steps.nope.result.f }}"}},
    ])
    out = await runner.dry_run(pb)
    assert out["status"] == "failed"
    assert "nope" in (out["error"] or "")
