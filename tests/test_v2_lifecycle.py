"""plans/032 phase 04 — propose = candidate for BOTH formats (docs/v2.md §9,
master §8): nothing runs live, no trigger arms, nothing is promoted until
playbook_publish walks the gate and the card. `live_version_of` is the one
implementation of "what is live" and returns None on a candidate-only row.
"""

from __future__ import annotations

import json

import pytest
from evidence import EXPLANATION, green_run
from sqlalchemy import select
from v2harness import CODE, PY_CODE, env

from plugin_playbooks import backfill_live_version, failure_digest
from plugin_playbooks import routes as routes_mod
from plugin_playbooks import versioning
from plugin_playbooks.fix_proposals import FixProposalService
from plugin_playbooks.models import (
    Playbook,
    PlaybookFixProposal,
    PlaybookRun,
    PlaybookVersion,
)
from plugin_playbooks.trigger_bindings import TriggerBindingService
from plugin_playbooks.triggers import PlaybookTriggerService

SOURCES = {"pblang": CODE, "python": PY_CODE}


async def _echo(**kw):
    return {"echoed": kw}


async def _row(sf, name="greeter") -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()


async def _publish(e, name="greeter", version=1) -> dict:
    await green_run(e.sf, version, name=name)
    return json.loads(await e.tools["playbook_publish"](name=name, explanation=EXPLANATION))


@pytest.mark.parametrize("fmt", ["pblang", "python"])
async def test_propose_returns_candidate_saved_exact_shape(fmt):
    e = await env(echo=_echo)
    try:
        out = json.loads(await e.tools["playbook_propose"](name="greeter", code=SOURCES[fmt]))
        expected = {
            "status": "candidate_saved", "live_version": None, "candidate_version": 1,
            "runnable_via": "playbook_run_candidate", "triggers_active": False,
            "publish_required": True,
        }
        assert {k: out.get(k) for k in expected} == expected, out
        assert out["format"] == fmt
        assert out["validated"] is True
        pb = await _row(e.sf)
        assert (pb.version, pb.live_version, pb.candidate_version) == (1, 0, 1)
        assert pb.format == fmt
        async with e.sf() as s:
            rows = (await s.execute(
                select(PlaybookVersion).where(PlaybookVersion.playbook_id == pb.id)
            )).scalars().all()
        assert [(r.version, r.author) for r in rows] == [(1, "agent")]
        # one implementation of "what is live"; the routes twin delegates
        # (the agent_tools twin is a build_tools closure — its delegation is
        # what test_run_refuses_naming_the_candidate exercises).
        assert versioning.live_version_of(pb) is None
        assert routes_mod._live_version_of(pb) is None
    finally:
        await e.dispose()


async def test_run_refuses_naming_the_candidate():
    e = await env(echo=_echo)
    try:
        await e.tools["playbook_propose"](
            name="greeter", code=CODE, agent_autonomy="agent_may_trigger",
        )
        out = json.loads(await e.tools["playbook_run"](name="greeter", inputs='{"greeting": "hi"}'))
        assert "candidate v1" in out["error"] and "playbook_run_candidate" in out["error"]
        assert out["publish_required"] is True
        assert out["candidate_version"] == 1
        async with e.sf() as s:
            assert (await s.execute(select(PlaybookRun))).scalars().all() == []
    finally:
        await e.dispose()


async def _archive(sf, name):
    async with sf() as s:
        pb = (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()
        pb.status = "archived"
        await s.commit()


async def test_recreate_no_longer_promotes():
    e = await env(echo=_echo)
    try:
        await e.tools["playbook_propose"](name="greeter", code=CODE)
        pub = await _publish(e)
        assert pub["status"] == "published", pub
        await _archive(e.sf, "greeter")
        new_code = CODE.replace("says hi", "says hello")
        out = json.loads(await e.tools["playbook_propose"](name="greeter", code=new_code))
        assert out["status"] == "candidate_saved"
        assert out["candidate_version"] == 2 and out["live_version"] == 1
        pb = await _row(e.sf)
        assert (pb.live_version, pb.candidate_version, pb.status) == (1, 2, "enabled")
        assert pb.code == CODE  # the row still holds the live v1 source

        # a never-published archived row is re-created; still nothing live
        await e.tools["playbook_propose"](name="fresh", code=CODE.replace("greeter", "fresh"))
        await _archive(e.sf, "fresh")
        out = json.loads(await e.tools["playbook_propose"](
            name="fresh", code=CODE.replace("greeter", "fresh"),
        ))
        assert out["status"] == "candidate_saved" and out["live_version"] is None
        pb = await _row(e.sf, "fresh")
        assert versioning.live_version_of(pb) is None
        assert pb.candidate_version == out["candidate_version"] == 2
    finally:
        await e.dispose()


async def test_publish_v1_of_new_playbook_goes_through_gate_and_card():
    e = await env(echo=_echo)
    try:
        await e.tools["playbook_propose"](name="greeter", code=CODE)
        refused = json.loads(await e.tools["playbook_publish"](name="greeter", explanation=EXPLANATION))
        assert refused.get("status") != "published"
        assert refused.get("gate") == "test_run", refused
        assert e.approvals.requests == []
        assert versioning.live_version_of(await _row(e.sf)) is None

        await green_run(e.sf, 1)
        out = json.loads(await e.tools["playbook_publish"](name="greeter", explanation=EXPLANATION))
        assert out["status"] == "published", out
        assert len(e.approvals.requests) == 1
        assert out["live_version"] == 1
        assert out["previous_live_version"] is None
        assert "FIRST live version" in out["note"]
        pb = await _row(e.sf)
        assert (pb.live_version, pb.candidate_version) == (1, None)
        assert versioning.live_version_of(pb) == 1
    finally:
        await e.dispose()


async def test_triggers_inactive_until_publish():
    # the publish probes gate wants every tool the python summary names
    e = await env(echo=_echo, fetch_list=_echo, send_message=_echo)
    try:
        out = json.loads(await e.tools["playbook_propose"](
            name="pyx", code=PY_CODE, triggers='[{"event": "x"}]',
        ))
        assert out["status"] == "candidate_saved" and out["triggers_active"] is False
        svc = PlaybookTriggerService(e.sf, e.bus, e.runner)
        await svc.start()
        assert svc._unsubs == {}
        assert e.bus.handlers == {}
        bindings = TriggerBindingService(e.sf, registry=None)
        assert await bindings._needed_events() == set()

        pub = await _publish(e, name="pyx")
        assert pub["status"] == "published", pub
        await svc.stop()
        await svc.start()
        assert set(svc._unsubs) == {"x"}
        assert await bindings._needed_events() == {"x"}
    finally:
        await e.dispose()


async def test_backfill_does_not_promote_candidate_only_rows():
    e = await env()
    try:
        async with e.sf() as s:
            s.add(Playbook(
                name="cand", display_name="cand", definition={"name": "cand", "steps": []},
                version=1, live_version=0, candidate_version=1, status="enabled",
            ))
            s.add(Playbook(
                name="legacy", display_name="legacy", definition={"name": "legacy", "steps": []},
                version=3, live_version=0, candidate_version=None, status="enabled",
            ))
            await s.commit()
        assert await backfill_live_version(e.sf) == 1
        cand, legacy = await _row(e.sf, "cand"), await _row(e.sf, "legacy")
        assert (cand.live_version, cand.candidate_version) == (0, 1)
        assert versioning.live_version_of(cand) is None
        assert legacy.live_version == 3
        assert versioning.live_version_of(legacy) == 3
        assert await backfill_live_version(e.sf) == 0
    finally:
        await e.dispose()


async def test_digest_and_fix_proposals_skip_live_none():
    e = await env(echo=_echo)
    try:
        await e.tools["playbook_propose"](name="greeter", code=CODE)
        pb = await _row(e.sf)
        async with e.sf() as s:
            run = PlaybookRun(
                playbook_id=pb.id, playbook_version=1, status="failed",
                trigger="schedule", is_test=False, error="boom",
            )
            s.add(run)
            await s.commit()
            await s.refresh(run)
            assert await failure_digest(s) == []
        await FixProposalService(e.sf, e.bus, None)._file_proposal_inner({"run_id": str(run.id)})
        async with e.sf() as s:
            assert (await s.execute(select(PlaybookFixProposal))).scalars().all() == []
    finally:
        await e.dispose()


async def test_rollback_and_ack_refuse_without_live():
    e = await env(echo=_echo)
    try:
        await e.tools["playbook_propose"](name="greeter", code=CODE)
        rb = json.loads(await e.tools["playbook_rollback"](name="greeter"))
        assert "no live version" in rb["error"]
        ack = json.loads(await e.tools["playbook_ack_failures"](name="greeter"))
        assert "no live version" in ack["error"]
        pb = await _row(e.sf)
        assert (pb.live_version, pb.candidate_version, pb.failures_acked_version) == (0, 1, None)
    finally:
        await e.dispose()
