"""0.30.3 — approval-parking tools must outlive the owner's decision.

`playbook_publish` / `playbook_rollback` raise the owner's approval card
INSIDE their handler and park until the decision arrives. With the ToolDef
default (30s) the runtime's `asyncio.wait_for` cancelled the parked handler
before any human could realistically answer: the card stayed pending
(orphaned — a late approval resumed nothing) and the publish never ran.
Found live in master plan 012 phase 4 E2E; pinned here so a refactor can't
silently drop the override back to the default.
"""

from __future__ import annotations

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks.agent_tools import build_tools

from test_versioned_specs import _Bus  # noqa: E402


def _tooldefs():
    return {td.name: td for td, _ in build_tools(None, _Bus(), None)}


def test_publish_and_rollback_park_long_enough_for_a_human():
    defs = _tooldefs()
    for name in ("playbook_publish", "playbook_rollback"):
        assert defs[name].timeout_seconds >= 300, (
            f"{name} parks on the owner's approval card — a short tool "
            "timeout cancels the parked handler and orphans the card"
        )
