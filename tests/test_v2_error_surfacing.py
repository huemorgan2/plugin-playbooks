"""plans/032 phase 04 — a python compute exception is readable everywhere
the agent looks (docs/v2.md §7): the playbook_run / playbook_run_candidate
result leads with the one-liner, playbook_status carries the four run
columns, playbook_runs and the failure digest name the failure, and the
fix-proposal signature keys on (line, error head). v1's failed-result text
is unchanged.
"""

from __future__ import annotations

import json

from evidence import EXPLANATION, green_run
from sqlalchemy import select
from v2harness import _effect, _error, env

from plugin_playbooks import failure_digest, render_failure_section
from plugin_playbooks.fix_proposals import FixProposalService, failure_signature
from plugin_playbooks.models import Playbook, PlaybookFixProposal, PlaybookRun

# line 7 is the `good = [...]` comprehension; line 9 the `total` sum.
PY_FAIL = '''async def run(ctx, inputs):
    fetch = await ctx.tool("fetch", url=inputs["url"])
    note = "scores above three"
    await ctx.log(note)
    if not inputs.get("url"):
        return {"good": 0}
    good = [r for r in fetch["items"] if r["score"] > 3]
    await ctx.log("filtered")
    total = sum(r["score"] for r in good)
    return {"good": len(good), "total": total}
'''
LINE_7 = PY_FAIL.splitlines()[6].strip()
LINE_9 = PY_FAIL.splitlines()[8].strip()
assert LINE_7.startswith("good = [") and LINE_9.startswith("total = sum(")

V1_BOOM = (
    "playbook(name='v1boom', description='fails')\n"
    "step = tool('boom', message=inputs.greeting)\n"
)

FABRICATE = (
    "Playbook execution FAILED. Do NOT fabricate results. "
    "Check the error details with playbook_status."
)


def _failing_script(line: int):
    """Spawn 1: the fetch effect; spawn 2: the canned KeyError at `line`."""
    def script(env: dict) -> dict:
        if len(env["journal"]) == 1:
            return _effect(1, "fetch", 1, "tool", "fetch", {"url": env["journal"][0]["inputs"].get("url")})
        return _error(
            "KeyError", "'items'", line=line,
            last={"seq": 1, "id": "fetch#1", "kind": "tool"},
        )
    return script


async def _fetch(**kw):
    return {"items": []}


async def _boom(**kw):
    raise RuntimeError("kaput")


async def _live(e, name="pyfail", code=PY_FAIL) -> Playbook:
    out = json.loads(await e.tools["playbook_propose"](
        name=name, code=code, agent_autonomy="agent_may_trigger",
    ))
    assert out["status"] == "candidate_saved", out
    await green_run(e.sf, 1, name=name)
    pub = json.loads(await e.tools["playbook_publish"](name=name, explanation=EXPLANATION))
    assert pub["status"] == "published", pub
    async with e.sf() as s:
        return (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()


async def _live_failed_run(e, pb) -> PlaybookRun:
    run = await e.runner.start_run_background(pb, inputs={"url": "u"}, trigger="schedule")
    row = await e.runner.wait_for_run(run.id, timeout=10)
    assert row is not None and row.status == "failed", row
    return row


async def test_compute_exception_visible_in_playbook_run_result():
    e = await env(script=_failing_script(7), fetch=_fetch)
    try:
        await _live(e)
        out = json.loads(await e.tools["playbook_run"](
            name="pyfail", inputs='{"url": "u"}', wait_seconds=5,
        ))
        assert out["status"] == "failed", out
        assert out["error"].startswith(f"line 7: {LINE_7} → KeyError: 'items' after effect fetch#1")
        assert "Do NOT fabricate" in out["error"]
        assert out["error_type"] == "KeyError"
        assert out["failed_at"]
        assert "error_detail" not in out

        # the candidate path leads with the same line
        read = await e.tools["playbook_edit"](name="pyfail")
        ticket = json.loads(read.split("\n", 1)[0])["ticket"]
        saved = json.loads(await e.tools["playbook_edit"](
            name="pyfail", ticket=ticket, code=PY_FAIL + "\n# v2\n",
        ))
        assert saved["status"] == "candidate_saved", saved
        cand = json.loads(await e.tools["playbook_run_candidate"](
            name="pyfail", inputs='{"url": "u"}', wait_seconds=5,
        ))
        assert cand["status"] == "failed", cand
        assert cand["error"].startswith(f"line 7: {LINE_7} → KeyError: 'items' after effect fetch#1")
        assert "Do NOT fabricate" in cand["error"]
        assert cand["error_type"] == "KeyError" and cand["failed_at"]
    finally:
        await e.dispose()


async def test_compute_exception_visible_in_status_runs_and_digest():
    e = await env(script=_failing_script(7), fetch=_fetch)
    try:
        pb = await _live(e)
        row = await _live_failed_run(e, pb)
        one_liner = f"line 7: {LINE_7} → KeyError: 'items' after effect fetch#1"
        assert row.error == one_liner

        status = json.loads(await e.tools["playbook_status"](run_id=str(row.id)))
        assert status["status"] == "failed"
        assert status["error"] == one_liner
        assert status["error_type"] == "KeyError"
        assert status["failed_at"]
        assert status["traceback"] and "KeyError: 'items'" in status["traceback"]
        assert "Traceback" in status["traceback"]

        runs = json.loads(await e.tools["playbook_runs"](name="pyfail"))
        entry = [r for r in runs["runs"] if r["run_id"] == str(row.id)][0]
        assert entry["status"] == "failed"
        assert entry["error"] == one_liner and entry["error_type"] == "KeyError"
        assert entry["failed_at"]

        async with e.sf() as s:
            digest = await failure_digest(s)
        assert len(digest) == 1
        assert digest[0]["name"] == "pyfail" and digest[0]["error"] == one_liner
        assert digest[0]["last_failed_run_id"] == str(row.id)
        assert one_liner in render_failure_section(digest)
    finally:
        await e.dispose()


async def _proposals(sf) -> list[PlaybookFixProposal]:
    async with sf() as s:
        return list((await s.execute(select(PlaybookFixProposal))).scalars().all())


async def test_failure_signature_dedupes_same_line():
    e = await env(script=_failing_script(7), fetch=_fetch)
    try:
        pb = await _live(e)
        svc = FixProposalService(e.sf, e.bus, None)
        first = await _live_failed_run(e, pb)
        second = await _live_failed_run(e, pb)
        await svc._file_proposal_inner({"run_id": str(first.id)})
        await svc._file_proposal_inner({"run_id": str(second.id)})
        rows = await _proposals(e.sf)
        assert len(rows) == 1
        assert rows[0].signature == failure_signature("pyfail", "line 7", "KeyError: 'items'")
        assert rows[0].failure_count == 2 and rows[0].last_run_id == second.id

        # the same error one line down is a different failure
        e.code_run._fn = _failing_script(9)
        third = await _live_failed_run(e, pb)
        assert third.error.startswith(f"line 9: {LINE_9} → KeyError: 'items'")
        await svc._file_proposal_inner({"run_id": str(third.id)})
        rows = await _proposals(e.sf)
        assert len(rows) == 2
        assert {r.signature for r in rows} == {
            failure_signature("pyfail", "line 7", "KeyError: 'items'"),
            failure_signature("pyfail", "line 9", "KeyError: 'items'"),
        }
    finally:
        await e.dispose()


async def test_v1_failed_result_text_unchanged():
    e = await env(boom=_boom)
    try:
        await _live(e, name="v1boom", code=V1_BOOM)
        out = json.loads(await e.tools["playbook_run"](
            name="v1boom", inputs='{"greeting": "hi"}', wait_seconds=10,
        ))
        assert out["status"] == "failed", out
        assert out["error"] == FABRICATE
        assert out["error_detail"] and "kaput" in out["error_detail"]
        assert out["error_type"] is None
        assert out["failed_at"]
    finally:
        await e.dispose()
