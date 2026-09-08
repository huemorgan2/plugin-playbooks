"""plans/032 phase 08 — v1/v2 PARITY: the same lifecycle surfaces seen
through a pblang twin and a python twin of the same playbook (docs/v2.md §7,
§11). Part 1 pins the completed-event shape and the handled-failure
contract; Part 2 extends `PAIRS` / `twin_env` with the remaining surfaces.

Real jail throughout: parity is a claim about the real runtimes, not about
a scripted `code_run`. Skipped without a usable jail.
"""

from __future__ import annotations

import json
from typing import Any

from evidence import EXPLANATION, green_run
from sqlalchemy import select
from v2harness import Env, env

from _jail import real_code_run, real_jail, requires_jail
from plugin_playbooks import failure_digest
from plugin_playbooks.fix_proposals import FixProposalService
from plugin_playbooks.models import PlaybookFixProposal, PlaybookRun

# the `playbook.run.completed` keys every subscriber has read since 0.44.0,
# in emit order, plus phase 08's additive LAST key
V1_COMPLETED_KEYS = [
    "run_id", "status", "duration_ms", "error", "playbook_id", "playbook_version",
    "is_test", "playbook_name", "trigger", "conversation_id", "parent_run_id",
    "wake_on_complete",
]
COMPLETED_KEYS = [*V1_COMPLETED_KEYS, "result"]


# ------------------------------------------------------------------ twins
# name → {"pblang": source, "python": source}; both twins call the same
# registered tools with the same inputs.
PAIRS: dict[str, dict[str, str]] = {
    "greeter": {
        "pblang": (
            "playbook(name='greeter', description='says hi')\n"
            "say = tool('echo', message=inputs.greeting)\n"
        ),
        "python": (
            "async def run(ctx, inputs):\n"
            "    say = await ctx.tool('echo', message=inputs['greeting'])\n"
            "    return say\n"
        ),
    },
    # a tool failure the code handles, then a tool that works
    "handled": {
        "pblang": (
            "playbook(name='handled', description='catches boom')\n"
            "b = tool('boom', on_error='continue')\n"
            "f = tool('fast')\n"
        ),
        "python": (
            "async def run(ctx, inputs):\n"
            "    try:\n"
            "        await ctx.tool('boom', _id='boom')\n"
            "    except ctx.EffectError:\n"
            "        pass\n"
            "    return await ctx.tool('fast', _id='fast')\n"
        ),
    },
}


async def _echo(**kw):
    return kw


async def _boom(**kw):
    raise RuntimeError("kaboom")


async def _fast(**kw):
    return {"ok": True}


TOOLS = {"echo": _echo, "boom": _boom, "fast": _fast}


async def twin_env(tmp_path, pair: str, fmt: str, *, publish: bool = True, **tools) -> Env:
    """A harness env (real jail) with the `fmt` twin of `pair` proposed as
    `pair` — and published as live v1 when `publish` (the default), so
    `playbook_run` drives a production run (trigger agent, is_test False)."""
    e = await env(**{**TOOLS, **tools})
    e.registry.add("code_run", real_code_run(tmp_path))
    out = json.loads(await e.tools["playbook_propose"](
        name=pair, code=PAIRS[pair][fmt], agent_autonomy="agent_may_trigger",
    ))
    assert out["status"] == "candidate_saved" and out["format"] == fmt, out
    if publish:
        await green_run(e.sf, 1, name=pair)
        out = json.loads(await e.tools["playbook_publish"](name=pair, explanation=EXPLANATION))
        assert out["status"] == "published", out
    return e


async def _live_run(e: Env, pair: str, inputs: dict[str, Any] | None = None) -> dict:
    out = json.loads(await e.tools["playbook_run"](
        name=pair, inputs=json.dumps(inputs or {}), wait_seconds=30,
    ))
    assert out["status"] in ("done", "failed"), out
    return out


def _completed(e: Env, run_id: str) -> dict:
    return next(p for p in e.bus.named("playbook.run.completed") if p["run_id"] == run_id)


async def _run_row(e: Env, run_id: str) -> PlaybookRun:
    import uuid

    async with e.sf() as s:
        return await s.get(PlaybookRun, uuid.UUID(run_id))


# ------------------------------------------------------------------ 1
@real_jail
@requires_jail()
async def test_completed_payload_is_the_v1_keys_plus_result(tmp_path):
    payloads: dict[str, dict] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "greeter", fmt)
        try:
            out = await _live_run(e, "greeter", {"greeting": "hi"})
            assert out["status"] == "done", out
            payloads[fmt] = _completed(e, out["run_id"])
            row = await _run_row(e, out["run_id"])
            assert row.format == fmt and row.is_test is False and row.trigger == "agent"
        finally:
            await e.dispose()
    for fmt, p in payloads.items():
        assert list(p) == COMPLETED_KEYS, (fmt, list(p))
        assert p["status"] == "done" and p["error"] is None
        assert p["playbook_name"] == "greeter" and p["playbook_version"] == 1
        assert p["trigger"] == "agent" and p["is_test"] is False
        assert p["parent_run_id"] is None and p["wake_on_complete"] is False
    # the one difference: what run() returned
    assert payloads["pblang"]["result"] is None
    assert payloads["python"]["result"] == {"message": "hi"}
    # every v1 key carries the same value on both twins (ids aside)
    for key in V1_COMPLETED_KEYS:
        if key in ("run_id", "playbook_id", "duration_ms"):
            continue
        assert payloads["pblang"][key] == payloads["python"][key], key


# ------------------------------------------------------------------ 2
@real_jail
@requires_jail()
async def test_handled_failure_is_a_green_run_in_status_runs_digest_and_proposals(tmp_path):
    e = await twin_env(tmp_path, "handled", "python")
    try:
        svc = FixProposalService(e.sf, e.bus)
        out = await _live_run(e, "handled")
        assert out["status"] == "done", out
        assert out["result"] == {"ok": True}
        run_id = out["run_id"]
        payload = _completed(e, run_id)
        assert payload["status"] == "done" and payload["error"] is None
        # playbook_status: a done run, no error hoisted; the caught failure
        # shows as `failed_handled` on its own row, WITH its error
        st = json.loads(await e.tools["playbook_status"](run_id=run_id))
        assert st["status"] == "done" and "error" not in st and "error_type" not in st, st
        assert st["result"] == {"ok": True}
        by_id = {s["step_id"]: s for s in st["steps"]}
        assert by_id["boom#1"]["status"] == "failed_handled"
        assert "kaboom" in (by_id["boom#1"]["error"] or "")
        assert by_id["fast#1"]["status"] == "done"
        # the DB step row itself stays `failed` (phase 06/07 pin it)
        async with e.sf() as s:
            from plugin_playbooks.models import PlaybookStepRun

            rows = (await s.execute(
                select(PlaybookStepRun).where(PlaybookStepRun.run_id == (await _run_row(e, run_id)).id)
            )).scalars().all()
        assert {r.step_id: r.status for r in rows} == {"boom#1": "failed", "fast#1": "done"}
        # playbook_runs: a done entry, no `failures`
        runs = json.loads(await e.tools["playbook_runs"](name="handled"))
        entry = next(r for r in runs["runs"] if r["run_id"] == run_id)
        assert entry["status"] == "done" and "failures" not in entry and "error" not in entry
        assert json.loads(await e.tools["playbook_runs"](name="handled", status="failed"))["count"] == 0
        # the failure digest: nothing failing
        async with e.sf() as s:
            assert await failure_digest(s) == []
        # the fix-proposal service: a done run files nothing
        await svc._on_completed(payload)
        assert not svc._tasks
        async with e.sf() as s:
            assert (await s.execute(select(PlaybookFixProposal))).scalars().all() == []
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 3-4 the publish gate (phase 08 part 2a)
async def _version_row(e: Env, name: str, version: int):
    from plugin_playbooks.models import Playbook, PlaybookVersion

    async with e.sf() as s:
        pb = (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()
        row = (await s.execute(select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == pb.id, PlaybookVersion.version == version,
        ))).scalar_one()
        return pb, row


@real_jail
@requires_jail()
async def test_gate_names_a_parked_candidate_run(tmp_path):
    """A candidate run parked on an owner card is neither green nor failed:
    the gate names the card and forbids a second run (Step 10)."""
    import uuid
    from datetime import datetime, timedelta, timezone

    from plugin_playbooks import publish

    e = await twin_env(tmp_path, "greeter", "python", publish=False)
    try:
        pb, row = await _version_row(e, "greeter", 1)
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        parked_on = {
            "kind": "approval", "approval_id": "7", "since": later.isoformat(),
            "due_at": None, "gate": "run",
        }
        async with e.sf() as s:
            run = PlaybookRun(
                playbook_id=pb.id, playbook_version=1, status="parked",
                trigger="agent-candidate", is_test=True, started_at=later,
                parked_on=parked_on,
            )
            s.add(run)
            await s.commit()
            run_id = run.id
        async with e.sf() as s:
            gate, refusal, evidence, failed = await publish.test_run_gate(s, pb.id, 1, row.created_at)
            assert await publish.latest_run_evidence(s, pb.id, 1, publish._aware(row.created_at)) is None
        assert evidence is None and failed is None
        assert gate["ok"] is False and gate["gate"] == "test_run"
        assert f"candidate run {run_id} of version 1 is parked on owner card #7" in gate["note"]
        body = json.loads(refusal)
        assert "parked on owner card #7" in body["error"] and str(run_id) in body["error"]
        assert "Do NOT start another candidate run" in body["hint"] and "nothing to poll" in body["hint"]
        assert body["run_id"] == str(run_id) and body["parked_on"] == parked_on
        # the refusal reaches the agent through playbook_publish unchanged
        out = json.loads(await e.tools["playbook_publish"](name="greeter", explanation=EXPLANATION))
        assert out["error"] == body["error"] and out["hint"] == body["hint"], out
        # not enforced: reported, never refused
        async with e.sf() as s:
            gate2, refusal2, _, _ = await publish.test_run_gate(s, pb.id, 1, row.created_at, require=False)
        assert refusal2 is None and gate2["enforced"] is False and "owner card #7" in gate2["note"]
        assert uuid.UUID(body["run_id"]) == run_id
    finally:
        await e.dispose()


@real_jail
@requires_jail()
async def test_gate_identical_for_green_and_failed_v2_candidate_runs(tmp_path):
    """Real python candidate runs feed the gate exactly like v1 evidence:
    a green run is the evidence, a failed run rides the failed slot only."""
    from plugin_playbooks import publish

    e = await twin_env(tmp_path, "greeter", "python", publish=False)
    try:
        out = json.loads(await e.tools["playbook_run_candidate"](
            name="greeter", inputs=json.dumps({"greeting": "hi"}), wait_seconds=30,
        ))
        assert out["status"] == "done" and out["candidate_version"] == 1, out
        green_id = out["run_id"]
        pb, row = await _version_row(e, "greeter", 1)
        async with e.sf() as s:
            gate, refusal, evidence, failed = await publish.test_run_gate(s, pb.id, 1, row.created_at)
        assert gate["ok"] is True and refusal is None and failed is None
        assert evidence is not None and str(evidence.id) == green_id
        assert evidence.is_test is True and evidence.status == "done"

        # a python candidate whose run fails (unhandled tool error)
        out = json.loads(await e.tools["playbook_propose"](
            name="failer", agent_autonomy="agent_may_trigger",
            code="async def run(ctx, inputs):\n    await ctx.tool('boom')\n",
        ))
        assert out["status"] == "candidate_saved" and out["format"] == "python", out
        out = json.loads(await e.tools["playbook_run_candidate"](name="failer", inputs="{}", wait_seconds=30))
        assert out["status"] == "failed", out
        failed_id = out["run_id"]
        pb2, row2 = await _version_row(e, "failer", 1)
        async with e.sf() as s:
            gate, refusal, evidence, failed = await publish.test_run_gate(
                s, pb2.id, 1, row2.created_at, require=False,
            )
        assert evidence is None                       # NEVER a failed run
        assert failed is not None and str(failed.id) == failed_id
        assert gate["ok"] is False and gate["enforced"] is False and refusal is None
        async with e.sf() as s:
            gate, refusal, evidence, failed = await publish.test_run_gate(s, pb2.id, 1, row2.created_at)
        assert evidence is None and str(failed.id) == failed_id
        assert gate["ok"] is False and refusal is not None
        assert "the latest test run of version 1 FAILED" in json.loads(refusal)["error"]
    finally:
        await e.dispose()
