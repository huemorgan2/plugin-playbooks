"""plans/032 phase 04 — the `format` column and the format-aware tools
(docs/v2.md §9): explicit > sniff > stored > python; a playbook's format
never changes through edit; python paths carry no pblang riders; a python
definition is the checker summary readers (probes, get_definition) use.
"""

from __future__ import annotations

import json

import pytest
from evidence import EXPLANATION, green_run
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


# a python twin of the pblang greeter on the same registered tool
PY_GREETER = (
    "async def run(ctx, inputs):\n"
    "    say = await ctx.tool('echo', message=inputs['greeting'])\n"
    "    return say\n"
)


async def test_edit_may_change_format():
    """plans/032 phase 08 rewrite of phase 04's
    `test_edit_default_is_stored_format_and_change_refused`: the default is
    still the stored format and an explicit mismatch is still refused, but a
    format change is no longer refused — the candidate carries its own
    `format` while the live version keeps its runtime until publish."""
    e = await env(echo=_echo)
    try:
        for name, code in (("py", PY_GREETER), ("pb", CODE.replace("greeter", "pb"))):
            out = json.loads(await e.tools["playbook_propose"](name=name, code=code))
            assert out["status"] == "candidate_saved", out
            await green_run(e.sf, 1, name=name)
            out = json.loads(await e.tools["playbook_publish"](name=name, explanation=EXPLANATION))
            assert out["status"] == "published", out
        # default = stored: prose sniffs nothing, so python stays python
        # (checker error) and pblang stays pblang (compile error)
        read = parse_read_stage(await e.tools["playbook_edit"](name="py"))
        assert read["format"] == "python" and read["live_format"] == "python"
        out = json.loads(await e.tools["playbook_edit"](name="py", ticket=read["ticket"], code=PROSE))
        assert out["saved"] is False and out["format"] == "python"
        assert out["errors"][0]["code"] == "v2-entry-point"
        read_pb = parse_read_stage(await e.tools["playbook_edit"](name="pb"))
        assert read_pb["format"] == "pblang" and read_pb["live_format"] == "pblang"
        out = json.loads(await e.tools["playbook_edit"](name="pb", ticket=read_pb["ticket"], code=PROSE))
        assert out["saved"] is False and out["format"] == "pblang"
        assert out["ticket_still_valid"] is True

        # an explicit format that contradicts the code is still refused
        out = json.loads(await e.tools["playbook_edit"](
            name="py", ticket=read["ticket"], code=PY_GREETER, format="pblang",
        ))
        assert out["saved"] is False and out["errors"][0]["code"] == "v2-format-mismatch"
        assert (await _row(e.sf, "py")).version == 1

        # a format change SAVES a candidate of the new format; live keeps its own
        out = json.loads(await e.tools["playbook_edit"](
            name="py", ticket=read["ticket"], code=CODE.replace("greeter", "py"),
        ))
        assert out["status"] == "candidate_saved", out
        assert out["format"] == "pblang" and out["live_format"] == "python"
        assert "candidate v2 is pblang; live v1 stays python until publish" in out["next"]
        out = json.loads(await e.tools["playbook_edit"](
            name="pb", ticket=read_pb["ticket"], code=PY_GREETER,
        ))
        assert out["status"] == "candidate_saved", out
        assert out["format"] == "python" and out["live_format"] == "pblang"
        assert "candidate v2 is python; live v1 stays pblang until publish" in out["next"]
        py, pb = await _row(e.sf, "py"), await _row(e.sf, "pb")
        assert (py.live_version, py.candidate_version, py.format) == (1, 2, "python")
        assert (pb.live_version, pb.candidate_version, pb.format) == (1, 2, "pblang")
        # the READ stage now shows both: the candidate's format and live's
        read = parse_read_stage(await e.tools["playbook_edit"](name="py"))
        assert read["format"] == "pblang" and read["live_format"] == "python"
        # publish flips the playbook's format to the promoted version's
        await green_run(e.sf, 2, name="py")
        out = json.loads(await e.tools["playbook_publish"](name="py", explanation=EXPLANATION))
        assert out["status"] == "published", out
        py = await _row(e.sf, "py")
        assert (py.live_version, py.format) == (2, "pblang")
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
        # plans/032 phase 05: python dry-runs on the segment loop (v2 shape);
        # the harness's scripted code_run returns straight away, so nothing
        # is exercised — the shape, not the semantics, is pinned here
        # (tests/test_v2_dry_run.py runs the real jail).
        dry = json.loads(await e.tools["playbook_dry_run"](name="py"))
        assert dry["dry_run"] is True and dry["format"] == "python"
        assert dry["status"] == "simulated_nothing_exercised"
        assert dry["steps_ran"] == {} and dry["tested_version"] == 1
        assert {s["id"] for s in dry["unreached_call_sites"]} == {"rows", "s", "approve", "send_message"}
        assert "ok" not in dry and "error" in dry and dry["error"] is None
    finally:
        await e.dispose()
