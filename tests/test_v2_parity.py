"""plans/032 phase 08 — v1/v2 PARITY: the same lifecycle surfaces seen
through a pblang twin and a python twin of the same playbook (docs/v2.md §7,
§11). Part 1 pins the completed-event shape and the handled-failure
contract; Part 2 extends `PAIRS` / `twin_env` with the remaining surfaces.

Real jail throughout: parity is a claim about the real runtimes, not about
a scripted `code_run`. Skipped without a usable jail.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from evidence import EXPLANATION, green_run
from fastapi import FastAPI
from readstage import parse_read_stage
from sqlalchemy import select
from test_wake_on_completion import OPS_ID, _Ctx as WakeCtx, _drain
from v2harness import Env, env

from _jail import real_code_run, real_jail, requires_jail
from plugin_playbooks import failure_digest, probes, publish, render_failure_section, routes
from plugin_playbooks import runner as runner_mod
from plugin_playbooks.card import render_delegation_card
from plugin_playbooks.delegation import _LIVE_FEEDS
from plugin_playbooks.fix_proposals import FixProposalService
from plugin_playbooks.models import (
    FAILED_RUN_STATUSES, Playbook, PlaybookDelegation, PlaybookEditTicket,
    PlaybookFixProposal, PlaybookProbeResult, PlaybookRun, PlaybookStepRun,
    PlaybookVersion,
)
from plugin_playbooks.trigger_bindings import TriggerBindingService
from plugin_playbooks.triggers import PlaybookTriggerService
from plugin_playbooks.versioning import get_version_row, heal_duplicate_version_rows, mint_version
from plugin_playbooks.wake import RunCompletionWake

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
            "    say = await ctx.tool('echo', message=inputs['greeting'], _id='say')\n"
            "    return say\n"
        ),
    },
    # an unhandled tool failure: the run fails at the `boom` step
    "failer": {
        "pblang": (
            "playbook(name='failer', description='blows up')\n"
            "boom = tool('boom')\n"
        ),
        "python": (
            "async def run(ctx, inputs):\n"
            "    await ctx.tool('boom', _id='boom')\n"
        ),
    },
    # a tool that takes a while (heartbeats)
    "slow": {
        "pblang": (
            "playbook(name='slow', description='waits')\n"
            "s = tool('slow')\n"
        ),
        "python": (
            "async def run(ctx, inputs):\n"
            "    return await ctx.tool('slow', _id='s')\n"
        ),
    },
    # a declared integer input (intake coercion) fired by a bus trigger
    "trig": {
        "pblang": (
            "playbook(name='trig', description='on tick',\n"
            "         inputs={'type': 'object', 'properties': {'n': {'type': 'integer'}}},\n"
            "         triggers=[trigger(event='parity.tick', map={'n': '{{ event.payload.n }}'})])\n"
            "r = tool('echo', n=inputs.n)\n"
        ),
        "python": (
            "async def run(ctx, inputs):\n"
            "    return await ctx.tool('echo', n=inputs['n'], _id='r')\n"
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


async def _slow(**kw):
    await asyncio.sleep(0.35)
    return {"ok": True}


TOOLS = {"echo": _echo, "boom": _boom, "fast": _fast, "slow": _slow}

INT_N = {"type": "object", "properties": {"n": {"type": "integer"}}}
TICK = [{"event": "parity.tick", "map": {"n": "{{ event.payload.n }}"}}]

# what the pblang header declares, passed as kwargs for the python twin
PY_PROPOSE: dict[str, dict[str, Any]] = {
    "greeter": {"description": "says hi"},
    "failer": {"description": "blows up"},
    "slow": {"description": "waits"},
    "trig": {
        "description": "on tick",
        "inputs_schema": json.dumps(INT_N), "triggers": json.dumps(TICK),
    },
}


async def twin_env(tmp_path, pair: str, fmt: str, *, publish: bool = True, **tools) -> Env:
    """A harness env (real jail) with the `fmt` twin of `pair` proposed as
    `pair` — and published as live v1 when `publish` (the default), so
    `playbook_run` drives a production run (trigger agent, is_test False)."""
    e = await env(**{**TOOLS, **tools})
    e.registry.add("code_run", real_code_run(tmp_path))
    extra = PY_PROPOSE.get(pair, {}) if fmt == "python" else {}
    out = json.loads(await e.tools["playbook_propose"](
        name=pair, code=PAIRS[pair][fmt], agent_autonomy="agent_may_trigger", **extra,
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


async def _playbook(e: Env, name: str) -> Playbook:
    async with e.sf() as s:
        return (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _norm(text: str) -> str:
    """Twin texts modulo the inevitable: ids, durations and the `#n`
    occurrence suffix a v2 step id carries (`say#1` vs `say`)."""
    text = _UUID_RE.sub("<uuid>", text)
    text = re.sub(r"after \d+s", "after Ns", text)
    text = re.sub(r"#\d+", "", text)
    return text


def _norm_errors(text: str) -> str:
    """v1 (abort text) and v2 (one-liner) word a failure differently — the
    parity claim is about the shape around it."""
    return re.sub(r"Error: .*", "Error: <error>", _norm(text))


async def _wake_msg(e: Env, payload: dict, **over) -> dict:
    ctx = WakeCtx()
    svc = RunCompletionWake(e.sf, e.bus, ctx)
    await svc._on_completed({**payload, **over})
    await _drain(svc)
    assert len(ctx.sent) == 1, ctx.sent
    return ctx.sent[0]


async def _proposals_settled(svc: FixProposalService) -> None:
    while svc._tasks:
        await asyncio.gather(*list(svc._tasks), return_exceptions=True)


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


# ================================================================== part 2b
# ------------------------------------------------------------------ 5 wake text
@real_jail
@requires_jail()
async def test_wake_text_identical_plus_result_line(tmp_path):
    """A python run's wake is the v1 wake plus one `Result:` block; a failed
    run's wake has the same honesty line on both twins."""
    done: dict[str, dict] = {}
    failed: dict[str, dict] = {}
    aware: dict[str, dict] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "greeter", fmt)
        try:
            out = await _live_run(e, "greeter", {"greeting": "hi"})
            payload = _completed(e, out["run_id"])
            origin = uuid.uuid4()
            done[fmt] = await _wake_msg(e, payload, wake_on_complete=True, conversation_id=str(origin))
            assert done[fmt]["conversation_id"] == origin
            # a background (trigger) run leaves an awareness note in ops
            aware[fmt] = await _wake_msg(e, payload, trigger="schedule", conversation_id=None)
            assert aware[fmt]["conversation_id"] == OPS_ID and aware[fmt]["channel"] == "awareness"
        finally:
            await e.dispose()
        e = await twin_env(tmp_path / f"{fmt}-f", "failer", fmt)
        try:
            out = await _live_run(e, "failer")
            assert out["status"] == "failed", out
            failed[fmt] = await _wake_msg(e, _completed(e, out["run_id"]), wake_on_complete=True)
            assert failed[fmt]["conversation_id"] == OPS_ID  # no origin → ops
        finally:
            await e.dispose()
    for d in done.values():
        assert d["title"] == "Playbook finished: greeter" and d["channel"] == "moment"
        assert "status 'done'" in d["content"] and "Step outputs:" in d["content"]
        assert '"message": "hi"' in d["content"]
    result_block = '\n\nResult:\n{\n  "message": "hi"\n}'
    assert "Result:" not in done["pblang"]["content"]
    assert result_block in done["python"]["content"]
    assert _norm(done["pblang"]["content"]) == _norm(done["python"]["content"].replace(result_block, ""))
    assert _norm(aware["pblang"]["content"]) == _norm(aware["python"]["content"])
    assert aware["pblang"]["title"] == aware["python"]["title"] == "Playbook run done: greeter"
    for f in failed.values():
        assert f["title"] == "Playbook finished: failer"
        assert "status 'failed'" in f["content"] and "Error: " in f["content"]
        assert "fabricate" in f["content"] and "Outcome unknown" not in f["content"]
    assert _norm_errors(failed["pblang"]["content"]) == _norm_errors(failed["python"]["content"])
    assert "kaboom" in failed["python"]["content"]


# ------------------------------------------------------------------ 6 fix proposals
@real_jail
@requires_jail()
async def test_fix_proposal_shape_identical(tmp_path):
    rows: dict[str, PlaybookFixProposal] = {}
    wakes: dict[str, list[dict]] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "failer", fmt)
        try:
            ctx = WakeCtx()
            svc = FixProposalService(e.sf, e.bus, ctx)
            ids = []
            for _ in range(2):
                out = await _live_run(e, "failer")
                assert out["status"] == "failed", out
                await svc._on_completed(_completed(e, out["run_id"]))
                await _proposals_settled(svc)
                ids.append(out["run_id"])
            async with e.sf() as s:
                found = (await s.execute(select(PlaybookFixProposal))).scalars().all()
            assert len(found) == 1, found  # dedupe: one open row per signature
            rows[fmt] = found[0]
            assert str(found[0].last_run_id) == ids[-1]
            wakes[fmt] = ctx.sent
        finally:
            await e.dispose()
    for fmt, row in rows.items():
        assert row.status == "open" and row.failure_count == 2, fmt
        assert row.diagnosis.startswith("Live run ") and " of version 1 failed at step 'boom" in row.diagnosis
    assert _norm(rows["pblang"].title) == _norm(rows["python"].title) == "Fix playbook 'failer': boom failing"
    for fmt, sent in wakes.items():
        assert [m["title"] for m in sent] == ["Playbook failing: failer"] * 2, fmt
        assert all(m["channel"] == "moment" and m["conversation_id"] == OPS_ID for m in sent)
        assert "It has now failed once." in sent[0]["content"]
        assert "It has now failed 2 times." in sent[1]["content"]
        assert "(blows up)" in sent[0]["content"] and "Outcome unknown" not in sent[0]["content"]
    for i in range(2):
        assert _norm_errors(wakes["pblang"][i]["content"]) == _norm_errors(wakes["python"][i]["content"])


# ------------------------------------------------------------------ 7 failure digest
@real_jail
@requires_jail()
async def test_failure_digest_identical(tmp_path):
    digests: dict[str, list[dict]] = {}
    sections: dict[str, str] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "failer", fmt)
        try:
            out = await _live_run(e, "failer")
            assert out["status"] == "failed", out
            async with e.sf() as s:
                digests[fmt] = await failure_digest(s)
            sections[fmt] = render_failure_section(digests[fmt], now=datetime.now(timezone.utc))
            runs = json.loads(await e.tools["playbook_runs"](name="failer", status="failed"))
            assert runs["count"] == 1
            entry = runs["runs"][0]
            assert entry["run_id"] == out["run_id"] and entry["format"] == fmt
            assert entry["failures"] and "kaboom" in json.dumps(entry["failures"])
        finally:
            await e.dispose()
    for fmt, d in digests.items():
        assert len(d) == 1, (fmt, d)
        assert d[0]["name"] == "failer" and d[0]["live_version"] == 1
        assert d[0]["failed"] == 1 and d[0]["finished"] == 1
        assert d[0]["last_failed_run_id"] and d[0]["last_failed_at"]
        assert "failer" in sections[fmt] and "OutcomeUnknown" not in sections[fmt]
    assert list(digests["pblang"][0]) == list(digests["python"][0])
    assert digests["python"][0]["error"].startswith("line 2: ")
    assert digests["python"][0]["error_type"] == "ToolError"
    assert _norm(sections["pblang"]).split("\n")[0] == _norm(sections["python"]).split("\n")[0]


# ------------------------------------------------------------------ 8 trust badges
@real_jail
@requires_jail()
async def test_trust_badges_from_checker_tool_list(tmp_path):
    for fmt, expected in (("pblang", ["echo"]), ("python", ["code_run", "echo"])):
        e = await twin_env(tmp_path / fmt, "greeter", fmt)
        try:
            pb = await _playbook(e, "greeter")
            assert probes.collect_tools(pb.definition) == expected
            if fmt == "python":
                assert pb.definition["tools"] == ["echo"]  # the checker's list + the jail
            async with e.sf() as s:
                pb = await s.get(Playbook, pb.id)
                pre = await probes.run_preflight(s, e.registry, pb, pb.definition)
                await s.commit()
                trust = await routes._trust_summaries(s, [pb.id])
                probed = (await s.execute(
                    select(PlaybookProbeResult.tool).where(PlaybookProbeResult.playbook_id == pb.id)
                )).scalars().all()
            assert pre["total"] == len(expected) and pre["failed"] == 0
            assert sorted(probed) == expected
            badge = trust[str(pb.id)]["probes"]
            assert badge["total"] == len(expected) and badge["failed"] == 0 and badge["probed_at"]
        finally:
            await e.dispose()


# ------------------------------------------------------------------ 9 playbook.saved + trigger resync
@real_jail
@requires_jail()
async def test_playbook_saved_emitted_on_publish_both_formats(tmp_path):
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "trig", fmt, publish=False)
        try:
            pb = await _playbook(e, "trig")
            assert pb.definition["triggers"][0]["event"] == "parity.tick"
            assert pb.inputs_schema == INT_N
            bindings = TriggerBindingService(e.sf, registry=None)
            assert e.bus.named("playbook.saved") == []
            assert await bindings._needed_events() == set()  # candidate-only: no live triggers
            ts = PlaybookTriggerService(e.sf, e.bus, e.runner)
            await ts.start()
            assert "parity.tick" not in e.bus.handlers
            await green_run(e.sf, 1, name="trig")
            out = json.loads(await e.tools["playbook_publish"](name="trig", explanation=EXPLANATION))
            assert out["status"] == "published", out
            assert e.bus.named("playbook.saved") == [{"name": "trig"}]
            assert await bindings._needed_events() == {"parity.tick"}
            ts2 = PlaybookTriggerService(e.sf, e.bus, e.runner)
            await ts2.start()
            assert len(e.bus.handlers["parity.tick"]) == 1
            await ts2.stop()
        finally:
            await e.dispose()


# ------------------------------------------------------------------ 10 heartbeats
@real_jail
@requires_jail()
async def test_heartbeats_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "HEARTBEAT_INTERVAL", 0.05)
    seen: dict[str, dict] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "slow", fmt)
        try:
            out = await _live_run(e, "slow")
            assert out["status"] == "done", out
            run_id = out["run_id"]
            started = [p for p in e.bus.named("activity.started") if p["activity_id"] == run_id]
            beats = [p for p in e.bus.named("activity.heartbeat") if p["activity_id"] == run_id]
            completed = [p for p in e.bus.named("activity.completed") if p["activity_id"] == run_id]
            assert len(started) == 1 and len(completed) == 1
            assert len(beats) >= 3, len(beats)  # 0.35 s tool / 0.05 s interval
            assert all(b == beats[0] for b in beats)
            names = [n for n, p in e.bus.events if p.get("activity_id") == run_id]
            assert names[0] == "activity.started" and names[-1] == "activity.completed"
            assert set(names[1:-1]) == {"activity.heartbeat"}
            seen[fmt] = {"started": started[0], "beat": beats[0], "completed": completed[0]}
        finally:
            await e.dispose()
    for key in ("started", "beat", "completed"):
        a = {**seen["pblang"][key], "activity_id": "<run>"}
        b = {**seen["python"][key], "activity_id": "<run>"}
        assert a == b, key
    assert seen["python"]["beat"] == {
        "activity_id": "<run>", "kind": "playbook", "label": "slow",
        "meta": {"playbook_name": "slow"},
    } | {"activity_id": seen["python"]["beat"]["activity_id"]}
    assert seen["python"]["completed"]["status"] == "done"


# ------------------------------------------------------------------ 11 report_to
class _RunCtx:
    def __init__(self, conversation) -> None:
        self.current_conversation_id = conversation

    async def ops_conversation_id(self):
        return OPS_ID


@real_jail
@requires_jail()
async def test_report_to_rules_identical(tmp_path):
    conv = uuid.uuid4()
    # (is_test, trigger, conversation) → report_to
    cases = [
        (True, "agent-candidate", conv, conv),
        (True, "agent-candidate", None, OPS_ID),
        (False, "agent", conv, conv),
        (False, "subtask:parent", conv, conv),
        (False, "agent", None, None),
        (False, "parity.tick", conv, None),
        (False, "schedule", None, None),
    ]
    tables: dict[str, list] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "greeter", fmt)
        try:
            pb = await _playbook(e, "greeter")
            table = []
            for is_test, trigger, conversation, expected in cases:
                e.runner._ctx = _RunCtx(conversation)
                run = await e.runner._create_run(pb, inputs={}, trigger=trigger, is_test=is_test)
                assert run.report_to == expected, (fmt, is_test, trigger, conversation, run.report_to)
                assert run.conversation_id == conversation and run.format == fmt
                table.append((is_test, trigger, run.conversation_id, run.report_to, run.playbook_version))
            e.runner._ctx = None
            tables[fmt] = table
        finally:
            await e.dispose()
    assert tables["pblang"] == tables["python"]


# ------------------------------------------------------------------ 12 trigger input shapes
@real_jail
@requires_jail()
async def test_trigger_input_shapes_identical(tmp_path):
    shapes: dict[str, dict] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "trig", fmt)
        try:
            ts = PlaybookTriggerService(e.sf, e.bus, e.runner)
            await ts.start()
            handler = e.bus.handlers["parity.tick"][0]
            # the map renders "7" (a string); the schema coerces it to 7
            await handler({"n": 7})
            started = e.bus.named("playbook.run.started")
            assert len(started) == 1
            run_id = uuid.UUID(started[0]["run_id"])
            done = await e.runner.wait_for_run(run_id, timeout=30)
            assert done is not None and done.status == "done", done
            row = await _run_row(e, str(run_id))
            assert row.trigger == "parity.tick" and row.is_test is False
            assert row.inputs == {"n": 7} and row.report_to is None and row.conversation_id is None
            async with e.sf() as s:
                steps = (await s.execute(
                    select(PlaybookStepRun).where(PlaybookStepRun.run_id == run_id)
                )).scalars().all()
            assert [(_norm(st.step_id), st.status, st.outputs["tool"]) for st in steps] == [("r", "done", "echo")]
            # the intake is identical (the row holds the coerced 7 on both);
            # what the TOOL sees differs by runtime design: a v1 template
            # (`n=inputs.n`) renders every scalar to text, a python call
            # passes the typed value (recorded in the phase 08 notes).
            assert steps[0].outputs["result"] == ({"n": 7} if fmt == "python" else {"n": "7"})
            # a shape the schema cannot coerce: loud intake, a failed row, no run
            await handler({"n": "seven"})
            # the rejection is RECORDED as a failed row (its started event
            # rides along) but no task ever drove it
            rec = e.bus.named("playbook.run.started")
            assert len(rec) == 2 and rec[1]["inputs"] == {"n": "seven"}
            assert uuid.UUID(rec[1]["run_id"]) not in e.runner._tasks
            async with e.sf() as s:
                rows = (await s.execute(
                    select(PlaybookRun).where(PlaybookRun.trigger == "parity.tick").order_by(PlaybookRun.started_at)
                )).scalars().all()
            assert [r.status for r in rows] == ["done", "failed"]
            bad = rows[1]
            assert bad.error_type == "InputTypeError" and "n" in bad.error and "integer" in bad.error
            assert bad.inputs == {"n": "seven"} and bad.format == fmt
            # the agent path coerces the same way and refuses the same shape
            out = await _live_run(e, "trig", {"n": "7"})
            assert out["status"] == "done" and (await _run_row(e, out["run_id"])).inputs == {"n": 7}
            rej = json.loads(await e.tools["playbook_run"](name="trig", inputs=json.dumps({"n": "x"})))
            assert rej["status"] == "rejected" and rej["input"] == "n" and rej["expected"] == "integer"
            await ts.stop()
            shapes[fmt] = {
                "completed": [
                    {k: v for k, v in p.items() if k not in ("run_id", "playbook_id", "duration_ms")}
                    for p in e.bus.named("playbook.run.completed") if p["trigger"] == "parity.tick"
                ],
                "bad_error": bad.error,
                "rejected": rej,
            }
        finally:
            await e.dispose()
    py, pb = shapes["python"], shapes["pblang"]
    assert pb["bad_error"] == py["bad_error"] and pb["rejected"] == py["rejected"]
    assert len(pb["completed"]) == len(py["completed"]) == 2
    for a, b in zip(pb["completed"], py["completed"]):
        assert list(a) == list(b) == COMPLETED_KEYS[2:] + [] or list(a) == list(b)
        assert {k: v for k, v in a.items() if k != "result"} == {k: v for k, v in b.items() if k != "result"}
    assert pb["completed"][0]["result"] is None and py["completed"][0]["result"] == {"n": 7}
    assert py["completed"][1]["result"] is None  # a rejected intake has no return value


# ------------------------------------------------------------------ 13 edit tickets
@real_jail
@requires_jail()
async def test_edit_tickets_identical(tmp_path):
    edits = {
        "pblang": PAIRS["greeter"]["pblang"].replace("says hi", "says hello"),
        "python": PAIRS["greeter"]["python"].replace("return say", "return {'said': say}"),
    }
    refusals: dict[str, list[str]] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "greeter", fmt, publish=False)
        try:
            edit = e.tools["playbook_edit"]
            got: list[str] = []
            a = parse_read_stage(await edit(name="greeter"))
            assert a["format"] == fmt and a["ticket"]
            got.append(json.loads(await edit(name="greeter", code=edits[fmt]))["error"])
            got.append(json.loads(await edit(name="greeter", ticket="nope", code=edits[fmt]))["error"])
            got.append(json.loads(await edit(name="greeter", ticket=str(uuid.uuid4()), code=edits[fmt]))["error"])
            b = parse_read_stage(await edit(name="greeter"))
            assert b["ticket"] != a["ticket"]
            out = json.loads(await edit(name="greeter", ticket=a["ticket"], code=edits[fmt]))
            assert out["status"] == "candidate_saved" and out["candidate_version"] == 2, out
            got.append(json.loads(await edit(name="greeter", ticket=a["ticket"], code=edits[fmt]))["error"])
            got.append(json.loads(await edit(name="greeter", ticket=b["ticket"], code=edits[fmt]))["error"])
            c = parse_read_stage(await edit(name="greeter"))
            async with e.sf() as s:
                row = await s.get(PlaybookEditTicket, uuid.UUID(c["ticket"]))
                row.created_at = datetime.now(timezone.utc) - timedelta(minutes=20)
                await s.commit()
            got.append(json.loads(await edit(name="greeter", ticket=c["ticket"], code=edits[fmt]))["error"])
            assert (await _playbook(e, "greeter")).version == 2  # only the valid write landed
            refusals[fmt] = got
        finally:
            await e.dispose()
    assert refusals["pblang"] == refusals["python"]
    heads = [r.split(". ")[0] + "." for r in refusals["python"]]
    assert heads == [
        "An edit ticket is required to save changes.",
        "Invalid edit ticket.",
        "Unknown edit ticket for this playbook.",
        "This edit ticket was already used.",
        "The playbook changed while you were editing (your ticket was issued for an older version).",
        "This edit ticket expired.",
    ]


# ------------------------------------------------------------------ 14 duplicate healing + mint above max
@real_jail
@requires_jail()
async def test_duplicate_healing_and_mint_above_max_on_python_rows(tmp_path):
    e = await twin_env(tmp_path, "greeter", "python")
    try:
        pb = await _playbook(e, "greeter")
        code = PAIRS["greeter"]["python"]
        async with e.sf() as s:
            # an OLDER code-less snapshot of v1 (the pre-0.32 edit path)
            s.add(PlaybookVersion(
                playbook_id=pb.id, version=1, definition=pb.definition, code=None,
                manifest=None, author="agent", message="snapshot", format="python",
                created_at=datetime.now(timezone.utc) - timedelta(days=1),
            ))
            await s.commit()
        async with e.sf() as s:
            pb = await s.get(Playbook, pb.id)
            picked = await get_version_row(s, pb, 1)
            assert picked.code == code and picked.message == "candidate"  # content wins over age
        assert await heal_duplicate_version_rows(e.sf) == 1
        assert await heal_duplicate_version_rows(e.sf) == 0
        async with e.sf() as s:
            rows = (await s.execute(
                select(PlaybookVersion).where(PlaybookVersion.playbook_id == pb.id)
            )).scalars().all()
            assert [(r.version, r.format, r.code == code, r.message) for r in rows] == [
                (1, "python", True, "candidate"),
            ]
            # the counter fell behind a stored row: mint ABOVE max(rows)
            s.add(PlaybookVersion(
                playbook_id=pb.id, version=3, definition=pb.definition, code=code,
                manifest="", author="agent", message="stray", format="python",
            ))
            pb = await s.get(Playbook, pb.id)
            pb.version = 1
            row = await mint_version(
                s, pb, definition=pb.definition, code=code, manifest="",
                author="agent", message="edit",
            )
            assert row.version == 4 and row.format == "python"  # None → the playbook's format
            other = await mint_version(
                s, pb, definition={"name": "greeter", "steps": []}, code="playbook(name='greeter')\n",
                manifest="", author="agent", message="twin", format="pblang",
            )
            assert other.version == 5 and other.format == "pblang"
            await s.commit()
        pb = await _playbook(e, "greeter")
        assert pb.version == 5 and pb.live_version == 1 and pb.format == "python"
        # the live run is untouched by the minted rows
        out = await _live_run(e, "greeter", {"greeting": "hi"})
        assert out["status"] == "done" and (await _run_row(e, out["run_id"])).playbook_version == 1
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 15 archived-name takeover across formats
async def _takeover(tmp_path, first: str, second: str) -> None:
    e = await twin_env(tmp_path, "greeter", first)
    try:
        pb = await _playbook(e, "greeter")
        pb_id = pb.id
        async with e.sf() as s:
            row = await s.get(Playbook, pb_id)
            row.status = "archived"
            await s.commit()
        extra = PY_PROPOSE["greeter"] if second == "python" else {}
        out = json.loads(await e.tools["playbook_propose"](
            name="greeter", code=PAIRS["greeter"][second], agent_autonomy="agent_may_trigger", **extra,
        ))
        assert out["status"] == "candidate_saved" and out["format"] == second, out
        assert out["candidate_version"] == 2 and out["live_version"] == 1
        pb = await _playbook(e, "greeter")
        assert pb.id == pb_id and pb.status == "enabled"
        # the LIVE side keeps its language until publish
        assert pb.format == first and pb.live_version == 1 and pb.candidate_version == 2
        assert pb.code == PAIRS["greeter"][first]
        _, v1 = await _version_row(e, "greeter", 1)
        _, v2 = await _version_row(e, "greeter", 2)
        assert (v1.format, v2.format) == (first, second)
        assert v2.code == PAIRS["greeter"][second]
        out = await _live_run(e, "greeter", {"greeting": "hi"})
        assert out["status"] == "done" and (await _run_row(e, out["run_id"])).format == first
        # publish flips the live format to the candidate row's
        await green_run(e.sf, 2, name="greeter")
        out = json.loads(await e.tools["playbook_publish"](name="greeter", explanation=EXPLANATION))
        assert out["status"] == "published", out
        pb = await _playbook(e, "greeter")
        assert pb.format == second and pb.live_version == 2 and pb.candidate_version is None
        assert pb.code == PAIRS["greeter"][second]
        out = await _live_run(e, "greeter", {"greeting": "hi"})
        assert out["status"] == "done", out
        row = await _run_row(e, out["run_id"])
        assert row.format == second and row.playbook_version == 2
        assert row.result == ({"message": "hi"} if second == "python" else None)
        assert e.bus.named("playbook.saved") == [{"name": "greeter"}] * 2
    finally:
        await e.dispose()


@real_jail
@requires_jail()
async def test_archived_name_takeover_across_formats(tmp_path):
    await _takeover(tmp_path / "p2y", "pblang", "python")
    await _takeover(tmp_path / "y2p", "python", "pblang")


# ------------------------------------------------------------------ 16 delegation card
CARD_TOKEN = "sekrit-token-parity"


@real_jail
@requires_jail()
async def test_delegation_card_token_flow_for_python_playbook(tmp_path):
    bodies: dict[str, dict] = {}
    for fmt in ("pblang", "python"):
        e = await twin_env(tmp_path / fmt, "greeter", fmt)
        try:
            routes.init_routes(e.sf, runner=None)
            row = PlaybookDelegation(
                task="tighten greeting", playbook="greeter", status="running",
                card_token=CARD_TOKEN, steps_used=1,
                events=[{"ts": "t", "phase": "Change", "kind": "tool",
                         "label": "playbook_edit", "detail": "", "ms": 40}],
            )
            async with e.sf() as s:
                s.add(row)
                await s.commit()
                await s.refresh(row)
            app = FastAPI()
            app.include_router(routes.ui_router)
            url = f"/api/p/plugin-playbooks/delegations/{row.id}/card"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://luna.test",
            ) as c:
                assert (await c.get(f"{url}?token=wrong")).status_code == 404
                assert (await c.get(url)).status_code == 404
                assert (await c.get(f"/api/p/plugin-playbooks/delegations/{uuid.uuid4()}/card?token={CARD_TOKEN}")).status_code == 404
                r = await c.get(f"{url}?token={CARD_TOKEN}")
            assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
            body = r.json()
            assert set(body) >= {"status", "playbook", "steps_used", "started_at", "finished_at", "result", "events"}
            assert body["playbook"] == "greeter" and body["status"] == "running" and body["result"] is None
            html = render_delegation_card(str(row.id), CARD_TOKEN, "greeter", "0.51.0")
            assert CARD_TOKEN in html and str(row.id) in html and "greeter" in html
            bodies[fmt] = {k: v for k, v in body.items() if k != "started_at"}
        finally:
            _LIVE_FEEDS.clear()
            await e.dispose()
    assert bodies["pblang"] == bodies["python"]


# ------------------------------------------------------------------ 17 timed_out_unknown / parked (operator decision)
ONE_LINER = (
    "line 2: say → OutcomeUnknown: outcome unknown — the server restarted "
    "while effect echo#1 was in flight before any effect"
)


async def _insert_run(e: Env, pb: Playbook, **fields) -> PlaybookRun:
    now = datetime.now(timezone.utc)
    base = dict(
        playbook_id=pb.id, playbook_version=1, trigger="schedule", is_test=False,
        started_at=now, format="python", inputs={"greeting": "hi"},
    )
    async with e.sf() as s:
        run = PlaybookRun(**{**base, **fields})
        s.add(run)
        await s.commit()
        await s.refresh(run)
        return run


def _event(run: PlaybookRun, **over) -> dict:
    base = {
        "run_id": str(run.id), "status": run.status, "duration_ms": 1200,
        "error": run.error, "playbook_id": str(run.playbook_id),
        "playbook_version": run.playbook_version, "is_test": run.is_test,
        "playbook_name": "greeter", "trigger": run.trigger, "conversation_id": None,
        "parent_run_id": None, "wake_on_complete": False, "result": None,
    }
    return {**base, **over}


@real_jail
@requires_jail()
async def test_timed_out_unknown_counts_as_failed_with_its_own_status(tmp_path):
    """Operator decision (owner may overrule): `timed_out_unknown` is a
    failure for the digest / fix proposals / the publish gate's failed slot,
    with `OutcomeUnknown` surfaced; `playbook_status` / `playbook_runs` /
    the wakes render it under its OWN status plus the outcome-unknown
    hint — it never masquerades as `failed`."""
    assert FAILED_RUN_STATUSES == ("failed", "timed_out_unknown")
    e = await twin_env(tmp_path, "greeter", "python")
    try:
        pb = await _playbook(e, "greeter")
        now = datetime.now(timezone.utc)
        tou = await _insert_run(
            e, pb, status="timed_out_unknown", error=ONE_LINER, error_type="OutcomeUnknown",
            completed_at=now, failed_at=now,
        )
        async with e.sf() as s:
            s.add(PlaybookStepRun(
                run_id=tou.id, step_id="say#1", step_kind="tool_call", status="failed",
                error="OutcomeUnknown: outcome unknown — the server restarted while effect echo#1 was in flight",
            ))
            await s.commit()
        # the enum the agent sees names it (luna-plugin.toml regeneration: Step 13)
        enum = e.defs["playbook_runs"].parameters["properties"]["status"]["enum"]
        assert "timed_out_unknown" in enum and "parked" in enum
        # failure digest: finished + failed, OutcomeUnknown named
        async with e.sf() as s:
            digest = await failure_digest(s)
        assert len(digest) == 1 and digest[0]["failed"] == 1 and digest[0]["finished"] == 1
        assert digest[0]["last_failed_run_id"] == str(tou.id)
        assert digest[0]["error_type"] == "OutcomeUnknown" and digest[0]["error"] == ONE_LINER
        section = render_failure_section(digest, now=now)
        assert "greeter" in section and "OutcomeUnknown:" in section
        # fix proposals: filed like a failure, the wake says outcome unknown
        ctx = WakeCtx()
        svc = FixProposalService(e.sf, e.bus, ctx)
        await svc._on_completed(_event(tou))
        await _proposals_settled(svc)
        async with e.sf() as s:
            props = (await s.execute(select(PlaybookFixProposal))).scalars().all()
        assert len(props) == 1 and props[0].last_run_id == tou.id
        assert "OutcomeUnknown" in props[0].diagnosis
        assert len(ctx.sent) == 1 and "Outcome unknown (OutcomeUnknown)" in ctx.sent[0]["content"]
        assert "check the target system" in ctx.sent[0]["content"]
        # playbook_status / playbook_runs: own status + hint, never `failed`
        st = json.loads(await e.tools["playbook_status"](run_id=str(tou.id)))
        assert st["status"] == "timed_out_unknown" and st["error_type"] == "OutcomeUnknown"
        assert st["hint"].startswith("Outcome unknown") and "Do NOT assume" in st["hint"]
        runs = json.loads(await e.tools["playbook_runs"](name="greeter", status="timed_out_unknown"))
        assert runs["count"] == 1 and runs["runs"][0]["run_id"] == str(tou.id)
        assert runs["runs"][0]["error_type"] == "OutcomeUnknown"
        assert runs["runs"][0]["hint"] == "outcome unknown — an effect's result was never recorded"
        assert json.loads(await e.tools["playbook_runs"](name="greeter", status="failed"))["count"] == 0
        # wakes: the failure lines plus the outcome-unknown sentence
        origin = uuid.uuid4()
        msg = await _wake_msg(e, _event(tou), wake_on_complete=True, conversation_id=str(origin))
        assert "status 'timed_out_unknown'" in msg["content"] and f"Error: {ONE_LINER}" in msg["content"]
        assert "Outcome unknown — an effect was in flight" in msg["content"]
        assert "Do NOT assume it did or did not happen" in msg["content"] and "fabricate" in msg["content"]
        note = await _wake_msg(e, _event(tou))  # background: awareness row in ops
        assert note["title"] == "Playbook run timed_out_unknown: greeter" and note["channel"] == "awareness"
        assert f"Error: {ONE_LINER}" in note["content"] and "Outcome unknown" in note["content"]
        # publish gate: a timed_out_unknown TEST run rides the failed slot, never the evidence slot
        out = json.loads(await e.tools["playbook_propose"](
            name="failer", code=PAIRS["failer"]["python"], agent_autonomy="agent_may_trigger",
        ))
        assert out["status"] == "candidate_saved", out
        pb2, row2 = await _version_row(e, "failer", 1)
        later = datetime.now(timezone.utc) + timedelta(seconds=5)
        tou2 = await _insert_run(
            e, pb2, status="timed_out_unknown", error=ONE_LINER, error_type="OutcomeUnknown",
            trigger="agent-candidate", is_test=True, started_at=later, completed_at=later,
        )
        async with e.sf() as s:
            gate, refusal, evidence, failed = await publish.test_run_gate(s, pb2.id, 1, row2.created_at)
        assert evidence is None and failed is not None and failed.id == tou2.id
        assert gate["ok"] is False and "outcome unknown" in gate["note"]
        assert "FAILED" in json.loads(refusal)["error"]
    finally:
        await e.dispose()


@real_jail
@requires_jail()
async def test_parked_is_never_finished_or_failed_in_any_consumer(tmp_path):
    """Operator decision (owner may overrule): a parked run is neither
    finished nor failed — the digest, fix proposals, runs list, status,
    wakes and the publish gate all say `parked`, never `failed`."""
    e = await twin_env(tmp_path, "greeter", "python")
    try:
        pb = await _playbook(e, "greeter")
        parked_on = {"kind": "approval", "approval_id": "9", "since": datetime.now(timezone.utc).isoformat(),
                     "due_at": None, "gate": "run"}
        parked = await _insert_run(e, pb, status="parked", parked_on=parked_on)
        # a real failure beside it, so the digest has a row to inspect
        failed = await _insert_run(
            e, pb, status="failed", error="line 2: say → ToolError: kaboom", error_type="ToolError",
            completed_at=datetime.now(timezone.utc),
        )
        async with e.sf() as s:
            digest = await failure_digest(s)
        assert len(digest) == 1 and digest[0]["failed"] == 1 and digest[0]["finished"] == 1
        assert digest[0]["last_failed_run_id"] == str(failed.id)
        assert "parked" not in render_failure_section(digest, now=datetime.now(timezone.utc))
        ctx = WakeCtx()
        svc = FixProposalService(e.sf, e.bus, ctx)
        await svc._on_completed(_event(parked))
        assert not svc._tasks and ctx.sent == []
        async with e.sf() as s:
            assert (await s.execute(select(PlaybookFixProposal))).scalars().all() == []
        st = json.loads(await e.tools["playbook_status"](run_id=str(parked.id)))
        assert st["status"] == "parked" and "error" not in st and "error_type" not in st
        assert st["parked_on"] == parked_on
        runs = json.loads(await e.tools["playbook_runs"](name="greeter", status="parked"))
        assert runs["count"] == 1 and "error" not in runs["runs"][0] and "hint" not in runs["runs"][0]
        assert json.loads(await e.tools["playbook_runs"](name="greeter", status="failed"))["count"] == 1
        # a parked row never emits a completion event; a stray one is not a failure wake
        msg = await _wake_msg(e, _event(parked), wake_on_complete=True, conversation_id=str(uuid.uuid4()))
        assert "status 'parked'" in msg["content"] and "Error:" not in msg["content"]
        assert "fabricate" not in msg["content"]
        # the publish gate: a parked run is neither evidence nor the failed
        # slot — the (newer) green test run is the evidence, the park is
        # invisible (a parked CANDIDATE run is named by test 3 above)
        _, row = await _version_row(e, "greeter", 1)
        async with e.sf() as s:
            ev = await publish.latest_run_evidence(s, pb.id, 1, None, include_live=True)
            assert ev is not None and ev.status == "done" and ev.id not in (parked.id, failed.id)
            gate, refusal, evidence, failed_run = await publish.test_run_gate(
                s, pb.id, 1, row.created_at, include_live=True,
            )
        assert gate["ok"] is True and refusal is None and failed_run is None
        assert evidence.id == ev.id and "parked" not in gate["note"]
    finally:
        await e.dispose()
