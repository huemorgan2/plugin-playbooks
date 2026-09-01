"""0.7.0 (luna 074/phase4) — kill the validate-instead-of-edit silent success.

Headless turns could only see playbook_validate (near-identical schema to
playbook_edit, success-shaped result) because playbook_edit was chat_only —
a scheduled "update the playbook" task validated and saved nothing while
reporting ok. Contract:

1. playbook_edit is NOT chat_only (headless callable; the dispatch/approval
   gate still fronts it in luna ≥ 0.40.003).
2. playbook_validate says loudly that nothing was saved.
"""

from __future__ import annotations

import json


class _Runner:
    _tools = None


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _tools():
    from plugin_playbooks.agent_tools import build_tools

    return {
        td.name: (td, handler)
        for td, handler in build_tools(lambda: _FakeSession(), None, _Runner())
    }


def test_playbook_edit_is_headless_callable():
    td, _ = _tools()["playbook_edit"]
    assert not getattr(td, "chat_only", False), (
        "playbook_edit must be visible to headless turns — chat_only hides it "
        "and leaves only the no-op validate twin"
    )


def test_no_mode_gated_tool_is_chat_only():
    """0.31.1 (BUG #3 rule): modes are the sole gate. chat_only hides a tool
    from EVERY headless turn — including muted ops wake turns, which is
    exactly where the fix flow runs (plan 0001 execution failed on this).
    Recursion protection for run-starting tools lives in
    _nested_run_refusal(), not chat_only."""
    tools = _tools()
    for name in (
        "playbook_propose", "playbook_run", "playbook_set_autonomy",
        "playbook_ack_failures", "playbook_dry_run", "playbook_run_candidate",
    ):
        assert not getattr(tools[name][0], "chat_only", False), (
            f"{name} must stay visible to muted/headless turns"
        )


import pytest


@pytest.mark.asyncio
async def test_run_tools_refuse_inside_a_playbook_run():
    """The 006.707 substitute: with chat_only gone, the run-starting tools
    refuse only the actually-recursive context (an agent_step inside a run),
    with steering toward `subtask` composition."""
    from plugin_playbooks import runner as runner_mod

    tools = _tools()
    token = runner_mod._active_run_id.set("run-123")
    try:
        for name in ("playbook_run", "playbook_run_candidate"):
            out = json.loads(await tools[name][1](name="pb"))
            assert out["gate"] == "nested_playbook_run", name
            assert "subtask" in out["hint"]
    finally:
        runner_mod._active_run_id.reset(token)


@pytest.mark.asyncio
async def test_validate_says_nothing_was_saved(monkeypatch):
    from plugin_playbooks import agent_tools

    async def _no_steps(session, exclude=None):
        return {}

    monkeypatch.setattr(agent_tools, "_load_all_playbook_steps", _no_steps)
    _, handler = _tools()["playbook_validate"]
    out = json.loads(await handler(definition_yaml="name: x\nsteps: []"))
    assert out.get("saved") is False
    assert "NOTHING was saved" in out.get("note", "")
