"""plans/027 — gated registration must never silently ungate.

Luna plan 102 phase 9.1 (round-2 context findings): `_register_tool` fell
back to UNGATED registration when the core lacked `skill_registry` or the
`skill_gated` kwarg — 18 authoring/delegation tools would silently appear
on every turn on such a core, defeating the skill gate. A core that cannot
gate must FAIL LOUD at load time so the operator sees a version mismatch,
not a quietly wider tool surface.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from luna_sdk import ToolDef

from plugin_playbooks import PlaybooksPlugin


class _Registry:
    def __init__(self, accept_skill_gated: bool = True) -> None:
        self.accept_skill_gated = accept_skill_gated
        self.calls: list[tuple[str, bool]] = []

    def register(self, plugin, tool_def, handler, **kw):
        if kw.get("skill_gated") and not self.accept_skill_gated:
            raise TypeError("register() got an unexpected keyword 'skill_gated'")
        self.calls.append((tool_def.name, bool(kw.get("skill_gated", False))))


async def _handler(**kwargs):
    return "ok"


def _plugin() -> PlaybooksPlugin:
    return PlaybooksPlugin.__new__(PlaybooksPlugin)


GATED = ToolDef(name="playbook_edit", description="t")
UNGATED = ToolDef(name="playbook_run", description="t")


def test_missing_skill_registry_fails_loud_not_ungated():
    reg = _Registry()
    ctx = SimpleNamespace(tool_registry=reg, skill_registry=None)
    with pytest.raises(RuntimeError, match="skill"):
        _plugin()._register_tool(ctx, GATED, _handler)
    assert reg.calls == [], "gated tool must not be registered ungated"


def test_typeerror_from_old_core_fails_loud_not_ungated():
    reg = _Registry(accept_skill_gated=False)
    ctx = SimpleNamespace(tool_registry=reg, skill_registry=object())
    with pytest.raises(RuntimeError, match="skill"):
        _plugin()._register_tool(ctx, GATED, _handler)
    assert reg.calls == [], "TypeError fallback must not ungate"


def test_modern_core_registers_gated():
    reg = _Registry()
    ctx = SimpleNamespace(tool_registry=reg, skill_registry=object())
    _plugin()._register_tool(ctx, GATED, _handler)
    assert reg.calls == [("playbook_edit", True)]


def test_non_gated_tool_registers_plain_even_without_skill_registry():
    reg = _Registry()
    ctx = SimpleNamespace(tool_registry=reg, skill_registry=None)
    _plugin()._register_tool(ctx, UNGATED, _handler)
    assert reg.calls == [("playbook_run", False)]
