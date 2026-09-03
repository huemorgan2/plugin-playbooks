"""Dry-run stub diagnostic (plans/016 follow-up).

When a spec leaves a tool_call/code step unstubbed, that step returns a
simulated dry-run placeholder ({_dry, _note}). Any downstream template that
reads a field off it fails. Previously the error said only
"a dict with keys: _dry, _note", which never told the author WHAT to do.
These tests pin the actionable form: name the unstubbed step(s) and say
"add a `stubs` entry".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.models import Base
from plugin_playbooks.pblang.compiler import compile_playbook
from plugin_playbooks.runner import (
    PlaybookRunner,
    _dry_stub_hint,
    _is_dry_placeholder,
)


def test_is_dry_placeholder():
    assert _is_dry_placeholder({"_dry": True, "_note": "simulated"})
    assert _is_dry_placeholder({"tool": "t", "result": {}, "_dry": True})
    assert not _is_dry_placeholder({"result": {"items": []}})
    assert not _is_dry_placeholder({"_dry": False})
    assert not _is_dry_placeholder("nope")
    assert not _is_dry_placeholder(None)


def test_dry_stub_hint_names_unstubbed_steps():
    ctx = SimpleNamespace(step_outputs={
        "find_dupes": {"tool": "monday_api_query", "result": {"_dry": True, "_note": "x"}, "_dry": True},
        "get_cols": {"tool": "monday_get_column_values", "result": {"column_values": []}},
    })
    hint = _dry_stub_hint("{{ steps.find_dupes.result.dupes.entries }}", ctx)
    assert "find_dupes" in hint
    assert "stubs" in hint
    # a reference to a properly-stubbed step contributes no hint
    assert _dry_stub_hint("{{ steps.get_cols.result.column_values }}", ctx) == ""


def test_dry_stub_hint_empty_when_no_placeholder():
    ctx = SimpleNamespace(step_outputs={"a": {"result": {"x": 1}}})
    assert _dry_stub_hint("{{ steps.a.result.x }}", ctx) == ""


class _Bus:
    async def emit(self, *_a, **_k):
        return None


@pytest.mark.asyncio
async def test_unstubbed_step_yields_actionable_dry_run_error():
    """End-to-end: an unstubbed tool step read downstream fails the dry-run
    with a message naming the step and telling the author to stub it."""
    class _Tool:
        def __init__(self, handler):
            self.handler = handler

    class _Tools:
        def __init__(self, **tools):
            self._tools = tools

        def get(self, name):
            return self._tools[name]

        def names(self):
            return list(self._tools)

    async def _noop(**_kw):
        return {"ok": True}

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(fetch_rows=_Tool(_noop), send_chat_message=_Tool(_noop)),
        events=_Bus(),
    )
    pb_def = compile_playbook(
        "playbook(name='diag', description='d')\n"
        "rows = tool('fetch_rows')\n"
        "gate = if_('{{ steps.rows.result.dupes.entries | length > 0 }}', then=[\n"
        "    tool('send_chat_message', message='hi'),\n"
        "])\n"
    )
    from plugin_playbooks.models import Playbook
    pb = Playbook(
        name="diag", display_name="diag", status="enabled",
        definition=pb_def.model_dump(mode="json", exclude_none=True, by_alias=True),
    )
    # rows is intentionally NOT stubbed -> placeholder -> gate read fails.
    out = await runner.dry_run(pb, inputs={}, stubs={})
    assert out["status"] == "failed"
    err = out.get("error") or ""
    assert "rows" in err
    assert "not stubbed" in err and "stubs" in err
    await engine.dispose()
