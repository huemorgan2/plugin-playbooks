"""Minimal `luna_sdk` stub so the plugin package imports without the Luna runtime.

`luna_sdk` is provided by the Luna runtime, not PyPI. We register a tiny fake
into sys.modules BEFORE plugin_playbooks is imported. declarative_base/UUID/JSONB
are backed by real SQLAlchemy so models.py still builds.
"""

from __future__ import annotations

import sys
import types
from contextvars import ContextVar
from typing import Any


def _install_luna_sdk_stub() -> None:
    if "luna_sdk" in sys.modules:
        return

    from sqlalchemy import JSON, Uuid
    from sqlalchemy.orm import DeclarativeBase

    mod = types.ModuleType("luna_sdk")

    class _Kwargs:
        def __init__(self, **kw: Any) -> None:
            self.__dict__.update(kw)

    class PluginManifest(_Kwargs):
        pass

    class SidebarSection(_Kwargs):
        pass

    class ToolDef(_Kwargs):
        pass

    class SkillDef(_Kwargs):
        pass

    class TriggerInfo(_Kwargs):
        pass

    class PluginContext:  # pragma: no cover - structural stand-in
        pass

    class ToolRegistry:  # pragma: no cover - structural stand-in
        pass

    class EventBus:  # pragma: no cover - structural stand-in
        pass

    class TriggerSourceRegistry:  # pragma: no cover - structural stand-in
        pass

    class LunaPlugin:  # pragma: no cover - structural stand-in
        manifest: Any

        async def on_load(self, ctx: Any) -> None: ...

    def declarative_base():
        class Base(DeclarativeBase):
            pass

        return Base

    _message_source: ContextVar[str | None] = ContextVar("message_source", default=None)

    def message_source() -> str | None:
        return _message_source.get()

    def get_current_user() -> Any:  # pragma: no cover - FastAPI dependency stand-in
        raise RuntimeError("stub: no auth outside the Luna runtime")

    mod.LunaPlugin = LunaPlugin
    mod.PluginContext = PluginContext
    mod.PluginManifest = PluginManifest
    mod.SidebarSection = SidebarSection
    mod.SkillDef = SkillDef
    mod.ToolDef = ToolDef
    mod.ToolRegistry = ToolRegistry
    mod.EventBus = EventBus
    mod.TriggerInfo = TriggerInfo
    mod.TriggerSourceRegistry = TriggerSourceRegistry
    mod.declarative_base = declarative_base
    mod.message_source = message_source
    mod.get_current_user = get_current_user
    mod.UUID = Uuid
    mod.JSONB = JSON
    sys.modules["luna_sdk"] = mod


_install_luna_sdk_stub()
