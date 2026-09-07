"""plans/032 phase 04 — the `format` column and the format-aware tools
(docs/v2.md §9): explicit > sniff > stored > python; a playbook's format
never changes through edit; python paths carry no pblang riders; a python
definition is the checker summary readers (probes, get_definition) use.
"""

from __future__ import annotations

import json

import pytest
from readstage import parse_read_stage
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from v2harness import CODE, PY_CODE, env

from plugin_playbooks import _ensure_columns
from plugin_playbooks.agent_tools import _PY_REFERENCE_LINE, _derive_code
from plugin_playbooks.models import Base, Playbook
from plugin_playbooks.probes import collect_tools
from plugin_playbooks.reference import LANGUAGE_CHEATSHEET, LANGUAGE_MINIREF

# neither a `playbook(...)` header nor an `async def run`: sniffs to nothing
# (valid Python, so the python default reaches the entry-point rule)
PROSE = "x = 1\n"


async def _echo(**kw):
    return kw


async def _row(sf, name) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()


async def _count(sf) -> int:
    async with sf() as s:
        return len((await s.execute(select(Playbook))).scalars().all())


async def test_format_column_migrates():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as s:
            s.add(Playbook(name="old", display_name="old", definition={"name": "old", "steps": []}))
            await s.commit()
        # an install that predates the column
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE playbooks DROP COLUMN format"))

        def _cols(sync_conn):
            return [c["name"] for c in inspect(sync_conn).get_columns("playbooks")]

        async with engine.connect() as conn:
            assert "format" not in await conn.run_sync(_cols)
        await _ensure_columns(engine)
        async with engine.connect() as conn:
            after_first = await conn.run_sync(_cols)
        assert "format" in after_first
        assert (await _row(sf, "old")).format == "pblang"
        await _ensure_columns(engine)  # idempotent: nothing added twice
        async with engine.connect() as conn:
            assert await conn.run_sync(_cols) == after_first
    finally:
        await engine.dispose()


@pytest.mark.parametrize("explicit,code,expect", [
    (None, PY_CODE, "python"),
    (None, CODE, "pblang"),
    ("pblang", PY_CODE, "v2-format-mismatch"),
    ("python", CODE, "v2-format-mismatch"),
    ("yaml", CODE, "v2-format-unknown"),
    (None, PROSE, "v2-entry-point"),
])
async def test_propose_precedence_table(explicit, code, expect):
    e = await env(echo=_echo)
    try:
        kw = {"format": explicit} if explicit is not None else {}
        out = json.loads(await e.tools["playbook_propose"](name="p", code=code, **kw))
        assert "format" in out, out
        if expect in ("python", "pblang"):
            assert out["status"] == "candidate_saved" and out["format"] == expect
            assert (await _row(e.sf, "p")).format == expect
        else:
            assert out.get("status") != "candidate_saved"
            assert out["errors"][0]["code"] == expect, out
            assert await _count(e.sf) == 0
            if expect == "v2-entry-point":
                assert out["format"] == "python"  # the default, no header sniffed
    finally:
        await e.dispose()


async def test_edit_default_is_stored_format_and_change_refused():
    e = await env(echo=_echo)
    try:
        for name, code in (("py", PY_CODE), ("pb", CODE.replace("greeter", "pb"))):
            out = json.loads(await e.tools["playbook_propose"](name=name, code=code))
            assert out["status"] == "candidate_saved", out
        # default = stored: prose sniffs nothing, so python stays python
        # (checker error) and pblang stays pblang (compile error)
        read = parse_read_stage(await e.tools["playbook_edit"](name="py"))
        assert read["format"] == "python"
        out = json.loads(await e.tools["playbook_edit"](name="py", ticket=read["ticket"], code=PROSE))
        assert out["saved"] is False and out["format"] == "python"
        assert out["errors"][0]["code"] == "v2-entry-point"
        read_pb = parse_read_stage(await e.tools["playbook_edit"](name="pb"))
        assert read_pb["format"] == "pblang"
        out = json.loads(await e.tools["playbook_edit"](name="pb", ticket=read_pb["ticket"], code=PROSE))
        assert out["saved"] is False and out["format"] == "pblang"
        assert out["ticket_still_valid"] is True

        # a format change is refused, ticket kept, nothing written
        out = json.loads(await e.tools["playbook_edit"](name="py", ticket=read["ticket"], code=CODE))
        assert out == {
            "stage": "write", "saved": False, "format": "python",
            "error": "This playbook is python; changing a playbook's format is "
                     "not supported yet — create a new playbook.",
            "ticket": read["ticket"], "ticket_still_valid": True,
        }
        out = json.loads(await e.tools["playbook_edit"](
            name="py", ticket=read["ticket"], code=PY_CODE, format="pblang",
        ))
        assert out["saved"] is False and out["errors"][0]["code"] == "v2-format-mismatch"
        out = json.loads(await e.tools["playbook_edit"](
            name="pb", ticket=read_pb["ticket"], code=PY_CODE,
        ))
        assert out["saved"] is False and out["format"] == "pblang"
        assert "changing a playbook's format" in out["error"]
        assert (await _row(e.sf, "py")).version == 1
        assert (await _row(e.sf, "pb")).version == 1
        assert (await _row(e.sf, "py")).format == "python"
        assert (await _row(e.sf, "pb")).format == "pblang"
    finally:
        await e.dispose()


async def test_validate_echoes_format():
    e = await env(echo=_echo)
    try:
        out = json.loads(await e.tools["playbook_validate"](code=PY_CODE))
        assert out == {**out, "ok": True, "format": "python", "saved": False}
        assert out["errors"] == []
        await e.tools["playbook_propose"](name="greeter", code=CODE)
        out = json.loads(await e.tools["playbook_validate"](name="greeter", code=CODE))
        assert out["ok"] is True and out["format"] == "pblang" and out["saved"] is False
        out = json.loads(await e.tools["playbook_validate"](name="greeter"))
        assert out["ok"] is True and out["format"] == "pblang"
        out = json.loads(await e.tools["playbook_validate"](definition_yaml="x: 1", format="python"))
        assert "pblang only" in out["error"] and out["format"] == "python"
        out = json.loads(await e.tools["playbook_validate"](code=CODE, format="yaml"))
        assert out["ok"] is False and out["errors"][0]["code"] == "v2-format-unknown"
    finally:
        await e.dispose()


async def test_riders_absent_on_python_paths():
    e = await env(echo=_echo)
    try:
        await e.tools["playbook_propose"](name="py", code=PY_CODE)
        await e.tools["playbook_propose"](name="pb", code=CODE.replace("greeter", "pb"))

        raw = await e.tools["playbook_edit"](name="py")
        read = parse_read_stage(raw)
        assert read["language_reference"] == _PY_REFERENCE_LINE
        assert "Rules agents forget" not in raw
        assert LANGUAGE_MINIREF not in raw
        bad = PY_CODE.replace('    return {"count"', '    print(1)\n    return {"count"')
        rejected = json.loads(await e.tools["playbook_edit"](name="py", ticket=read["ticket"], code=bad))
        assert rejected["saved"] is False and "language_reference" not in rejected
        invalid = json.loads(await e.tools["playbook_validate"](code=bad))
        assert invalid["ok"] is False and "language_reference" not in invalid
        refused = json.loads(await e.tools["playbook_propose"](name="py2", code=bad))
        assert refused.get("status") != "candidate_saved" and "language_reference" not in refused

        raw_pb = await e.tools["playbook_edit"](name="pb")
        assert LANGUAGE_MINIREF in raw_pb
        assert "Rules agents forget" in raw_pb
        invalid_pb = json.loads(await e.tools["playbook_validate"](code="playbook(name='x')\nq = tool(\n"))
        assert invalid_pb["ok"] is False
        assert invalid_pb["language_reference"] == LANGUAGE_CHEATSHEET
    finally:
        await e.dispose()


async def test_tool_descriptions_are_format_aware():
    e = await env()
    try:
        for name in ("playbook_propose", "playbook_edit", "playbook_validate"):
            td = e.defs[name]
            assert "python" in td.description.lower(), name
            assert "pblang" in td.description.lower(), name
            assert td.parameters["properties"]["format"]["enum"] == ["pblang", "python"], name
    finally:
        await e.dispose()


async def test_python_definition_summary_serves_readers():
    e = await env(echo=_echo)
    try:
        out = json.loads(await e.tools["playbook_propose"](
            name="py", code=PY_CODE, triggers='[{"event": "x"}]',
            inputs_schema='{"type": "object", "properties": {"url": {"type": "string"}}}',
        ))
        assert out["status"] == "candidate_saved", out
        pb = await _row(e.sf, "py")
        d = pb.definition
        assert d["name"] == "py" and d["format"] == "python"
        assert d["triggers"] == [{"event": "x"}]
        assert d["inputs"] == {"type": "object", "properties": {"url": {"type": "string"}}}
        assert set(d["tools"]) == {"fetch_list", "send_message"}
        assert d["call_sites"]
        assert "steps" not in d
        assert collect_tools(d) == sorted(set(d["tools"]) | {"code_run"})
        assert _derive_code(pb) == PY_CODE
        assert json.loads(await e.tools["playbook_get_definition"](name="py", format="json")) == d
        assert await e.tools["playbook_get_definition"](name="py") == PY_CODE
        dry = json.loads(await e.tools["playbook_dry_run"](name="py"))
        assert dry["ok"] is False and "not available yet" in dry["error"]
        assert dry["format"] == "python"
    finally:
        await e.dispose()
