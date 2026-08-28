"""plans/011 — the /reference/icons endpoint.

build_icon_reference maps every registered tool to its owning plugin and
every advertised trigger to its publisher plugin; the route memoises the
payload. Everything degrades to empty sections, never an exception.
"""

from __future__ import annotations

import pytest

from plugin_playbooks import routes
from plugin_playbooks.routes import build_icon_reference


class _ToolDef:
    def __init__(self, name: str) -> None:
        self.name = name


class _RegisteredTool:
    def __init__(self, name: str, plugin: str | None) -> None:
        self.definition = _ToolDef(name)
        self.plugin = plugin


class _ToolRegistry:
    def __init__(self, tools: list[_RegisteredTool]) -> None:
        self._list = tools

    def all(self) -> list[_RegisteredTool]:
        return self._list


class _Info:
    def __init__(self, event_pattern: str, source: str, app: str, label: str) -> None:
        self.event_pattern = event_pattern
        self.source = source
        self.app = app
        self.label = label


class _Source:
    def __init__(self, name: str) -> None:
        self.source_name = name


class _TriggerSources:
    def __init__(self, by_plugin: dict, infos: list[_Info], fail: bool = False) -> None:
        self._by_plugin = by_plugin
        self._infos = infos
        self._fail = fail

    async def all_triggers(self, app=None):
        if self._fail:
            raise RuntimeError("provider down")
        return self._infos


@pytest.mark.asyncio
async def test_tools_map_to_their_owning_plugin():
    registry = _ToolRegistry([
        _RegisteredTool("monday_boards_list", "plugin-monday"),
        _RegisteredTool("web_search", "plugin-web-access"),
        _RegisteredTool("orphan", None),  # no plugin → omitted
    ])
    out = await build_icon_reference(registry, None)
    assert out["tools"] == {
        "monday_boards_list": "plugin-monday",
        "web_search": "plugin-web-access",
    }
    assert out["triggers"] == []


@pytest.mark.asyncio
async def test_triggers_resolve_publisher_plugin_via_registry_ownership():
    sources = _TriggerSources(
        by_plugin={
            "plugin-connectors": [_Source("connectors")],
            "plugin-monday": [_Source("monday")],
        },
        infos=[
            _Info("connector.gmail.new_gmail_message", "connectors", "gmail", "New email"),
            _Info("monday.item_created", "monday", "monday", "Item created"),
            _Info("mystery.event", "unknown-source", "x", "?"),
        ],
    )
    out = await build_icon_reference(None, sources)
    assert out["tools"] == {}
    by_event = {t["event_pattern"]: t for t in out["triggers"]}
    assert by_event["connector.gmail.new_gmail_message"]["plugin"] == "plugin-connectors"
    assert by_event["connector.gmail.new_gmail_message"]["app"] == "gmail"
    assert by_event["monday.item_created"]["plugin"] == "plugin-monday"
    # unknown publisher → null plugin, entry still listed (UI keeps the glyph)
    assert by_event["mystery.event"]["plugin"] is None


@pytest.mark.asyncio
async def test_registry_failures_yield_empty_sections_not_errors():
    class _Broken:
        def all(self):
            raise RuntimeError("boom")

    sources = _TriggerSources(by_plugin={}, infos=[], fail=True)
    out = await build_icon_reference(_Broken(), sources)
    assert out == {"tools": {}, "triggers": []}

    out = await build_icon_reference(None, None)
    assert out == {"tools": {}, "triggers": []}


@pytest.mark.asyncio
async def test_route_memoises_and_reset_clears(monkeypatch):
    calls = {"n": 0}

    class _CountingRegistry:
        def all(self):
            calls["n"] += 1
            return [_RegisteredTool("t1", "plugin-x")]

    class _Runner:
        _tools = _CountingRegistry()

    monkeypatch.setattr(routes, "_runner", _Runner())
    monkeypatch.setattr(routes, "_trigger_sources", None)
    routes._reset_icon_cache()

    first = await routes.icon_reference()
    second = await routes.icon_reference()
    assert first == second == {"tools": {"t1": "plugin-x"}, "triggers": []}
    assert calls["n"] == 1  # served from cache

    routes._reset_icon_cache()
    await routes.icon_reference()
    assert calls["n"] == 2
