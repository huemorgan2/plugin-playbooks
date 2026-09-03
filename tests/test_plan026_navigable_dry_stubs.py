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
