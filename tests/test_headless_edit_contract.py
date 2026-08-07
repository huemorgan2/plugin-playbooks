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


def test_agent_step_and_dry_run_stay_chat_only():
    tools = _tools()
    assert getattr(tools["playbook_dry_run"][0], "chat_only", False)


import pytest


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
