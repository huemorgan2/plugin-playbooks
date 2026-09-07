"""plans/032 phase 04 — the edit write-stage payload (docs/v2.md §9): a
rejected write keeps the ticket and says so in one machine-readable shape;
a green write says it was validated and where things stand (no live
version yet → publish is the next step). Nothing says "single-use" anymore.
"""

from __future__ import annotations

import json
import uuid

from readstage import parse_read_stage
from sqlalchemy import select
from v2harness import CODE, PY_CODE, env

from plugin_playbooks.agent_tools import _EDIT_RETRY_TEXT
from plugin_playbooks.models import Playbook, PlaybookEditTicket
from plugin_playbooks.reference import LANGUAGE_CHEATSHEET

REJECTED_KEYS = {
    "stage", "saved", "format", "errors", "warnings", "ticket",
    "ticket_still_valid", "expires_in_seconds", "retry",
}

PY_PRINT = PY_CODE.replace(
    '    return {"count": len(summaries)}',
    '    print(summaries)\n    return {"count": len(summaries)}',
)
PY_LOG = PY_CODE.replace(
    '    return {"count": len(summaries)}',
    '    await ctx.log("done")\n    return {"count": len(summaries)}',
)
assert PY_PRINT != PY_CODE and PY_LOG != PY_CODE


async def _echo(**kw):
    return kw


async def _proposed(fmt: str):
    e = await env(echo=_echo)
    code = PY_CODE if fmt == "python" else CODE
    out = json.loads(await e.tools["playbook_propose"](name="greeter", code=code))
    assert out["status"] == "candidate_saved", out
    read = parse_read_stage(await e.tools["playbook_edit"](name="greeter"))
    return e, read


async def _ticket_row(sf, ticket: str) -> PlaybookEditTicket:
    async with sf() as s:
        return await s.get(PlaybookEditTicket, uuid.UUID(ticket))


async def _version(sf) -> int:
    async with sf() as s:
        return (await s.execute(select(Playbook))).scalar_one().version


async def test_checker_error_on_edit_keeps_ticket():
    e, read = await _proposed("python")
    try:
        assert read["format"] == "python"
        out = json.loads(await e.tools["playbook_edit"](
            name="greeter", ticket=read["ticket"], code=PY_PRINT,
        ))
        assert set(out) == REJECTED_KEYS, sorted(out)
        assert out["stage"] == "write" and out["saved"] is False
        assert out["format"] == "python"
        assert out["ticket"] == read["ticket"] and out["ticket_still_valid"] is True
        assert 0 < out["expires_in_seconds"] <= 900
        assert out["retry"] == _EDIT_RETRY_TEXT
        assert out["errors"][0]["code"] == "v2-use-ctx-log"
        assert out["errors"][0]["severity"] == "error"
        assert out["errors"][0]["example_fix"]
        assert "language_reference" not in out
        assert await _version(e.sf) == 1
        assert (await _ticket_row(e.sf, read["ticket"])).used_at is None
    finally:
        await e.dispose()


async def test_same_ticket_reusable_after_checker_error():
    e, read = await _proposed("python")
    try:
        first = json.loads(await e.tools["playbook_edit"](
            name="greeter", ticket=read["ticket"], code=PY_PRINT,
        ))
        assert first["saved"] is False and first["ticket_still_valid"] is True
        second = json.loads(await e.tools["playbook_edit"](
            name="greeter", ticket=read["ticket"], code=PY_LOG,
        ))
        assert second["status"] == "candidate_saved", second
        assert second["candidate_version"] == 2
        assert second["validated"] is True
        assert await _version(e.sf) == 2
        assert (await _ticket_row(e.sf, read["ticket"])).used_at is not None
        third = json.loads(await e.tools["playbook_edit"](
            name="greeter", ticket=read["ticket"], code=PY_LOG,
        ))
        assert "error" in third and "already used" in third["error"]
        assert await _version(e.sf) == 2
    finally:
        await e.dispose()


async def test_compile_error_on_pblang_edit_keeps_ticket_with_cheatsheet():
    e, read = await _proposed("pblang")
    try:
        assert read["format"] == "pblang"
        out = json.loads(await e.tools["playbook_edit"](
            name="greeter", ticket=read["ticket"],
            code="playbook(name='greeter')\nsay = tool(\n",
        ))
        # `error` rides along: the pre-032 "does not compile" sentence that
        # older callers key on (recorded as a phase 04 deviation).
        assert set(out) == REJECTED_KEYS | {"language_reference", "error"}, sorted(out)
        assert out["saved"] is False and out["format"] == "pblang"
        assert out["ticket"] == read["ticket"] and out["ticket_still_valid"] is True
        assert 0 < out["expires_in_seconds"] <= 900
        assert out["retry"] == _EDIT_RETRY_TEXT
        assert out["errors"]
        assert out["language_reference"] == LANGUAGE_CHEATSHEET
        assert await _version(e.sf) == 1
        assert (await _ticket_row(e.sf, read["ticket"])).used_at is None
        # and the same ticket still writes
        again = json.loads(await e.tools["playbook_edit"](
            name="greeter", ticket=read["ticket"], code=CODE.replace("says hi", "says hey"),
        ))
        assert again["status"] == "candidate_saved" and again["candidate_version"] == 2
    finally:
        await e.dispose()


async def test_green_write_says_validated():
    e, read = await _proposed("python")
    try:
        out = json.loads(await e.tools["playbook_edit"](
            name="greeter", ticket=read["ticket"], code=PY_LOG,
        ))
        assert out["status"] == "candidate_saved"
        assert out["validated"] is True
        assert out["format"] == "python"
        assert out["live_version"] is None and out["candidate_version"] == 2
        assert "do not call playbook_validate" in out["next"].lower()
        assert "playbook_publish" in out["next"]
        assert "playbook_run_candidate" in out["next"]
    finally:
        await e.dispose()


async def test_ticket_text_no_longer_says_single_use():
    e, read = await _proposed("python")
    try:
        assert "single-use" not in read["instructions"]
        assert "single use" not in read["instructions"].lower()
        assert "keeps it valid" in read["instructions"]
        desc = e.defs["playbook_edit"].description
        assert "single-use" not in desc and "single use" not in desc.lower()
    finally:
        await e.dispose()
