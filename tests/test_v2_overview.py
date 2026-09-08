"""plans/032 phase 09 — `playbook_overview`, the derived truth surface for
one playbook (master §2 "Playbook overview"; M4 rule: a parked run is never
finished / failed), and the `next` hints that point to it."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from _provenance_env import add_candidate, call, live_with_candidate, make_env, publish_v1
from evidence import EXPLANATION
from readstage import parse_read_stage
from sqlalchemy import select
from test_manifest_drift import _Bus, _StubRunner
from test_repro_fixplaybooks_lifecycle import CODE, NEW_CODE, _Approvals

from plugin_playbooks import PlaybooksPlugin
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Playbook, PlaybookRun, PlaybookVersion
from plugin_playbooks.provenance import overview_hint

KEYS = [
    "playbook", "format", "playbook_run_executes", "candidate",
    "runs_of_live_since_publish", "parked_runs", "pending_approvals",
    "autonomy", "versions", "more", "next",
]
HINT = overview_hint("greeter")


async def test_fresh_playbook_shape():
    """propose only: candidate v1, nothing live, one version."""
    env = await make_env()
    out = await call(env, "playbook_propose", name="greeter", code=CODE)
    assert out["status"] == "candidate_saved"
    out = await call(env, "playbook_overview", name="greeter")
    assert list(out) == KEYS
    assert "kind" not in out  # an overview is not a run
    cand = out["candidate"]
    assert cand["version"] == 1 and cand["last_test_run"] is None
    assert cand["saved_at"] and cand["author"]
    assert out == {
        "playbook": "greeter",
        "format": "pblang",
        "playbook_run_executes": {
            "version": None,
            "reason": "no live version — candidate-only; playbook_run refuses, "
                      "use playbook_run_candidate",
        },
        "candidate": cand,
        "runs_of_live_since_publish": 0,
        "parked_runs": [],
        "pending_approvals": [],
        "autonomy": "agent_must_confirm",  # the default
        "versions": [{
            "version": 1, "created_at": out["versions"][0]["created_at"],
            "author": cand["author"], "message": out["versions"][0]["message"],
            "promoted_from": None, "live": False, "candidate": True,
        }],
        "more": {"parked_runs": 0, "pending_approvals": 0, "versions": 0},
        "next": "Candidate v1 is not live: test it with "
                "playbook_run_candidate(name='greeter'), then "
                "playbook_publish(name='greeter') to make it live.",
    }
    assert (await call(env, "playbook_overview", name="nope")) == {
        "error": "Playbook 'nope' not found"
    }


async def test_candidate_only_shape():
    """propose + a candidate test run: last_test_run is that run; a live run
    is still refused."""
    env = await make_env()
    await call(env, "playbook_propose", name="greeter", code=CODE,
               agent_autonomy="agent_may_trigger")
    run = await call(env, "playbook_run_candidate", name="greeter",
                     inputs='{"greeting": "hi"}', wait_seconds=1)
    assert run["kind"] == "candidate_test_run" and run["status"] == "done"
    out = await call(env, "playbook_overview", name="greeter")
    assert out["playbook_run_executes"]["version"] is None
    assert out["candidate"]["version"] == 1
    assert out["candidate"]["last_test_run"] == {
        "run_id": run["run_id"], "status": "done",
        "at": out["candidate"]["last_test_run"]["at"],
    }
    assert out["candidate"]["last_test_run"]["at"]
    assert out["runs_of_live_since_publish"] == 0  # a test run of nothing live
    assert out["versions"][0]["candidate"] is True and out["versions"][0]["live"] is False
    # promote: live v1, no candidate, the test run does not count as a real run
    out = await call(env, "playbook_publish", name="greeter", explanation=EXPLANATION)
    assert out["status"] == "published" and out["next"] == HINT
    out = await call(env, "playbook_overview", name="greeter")
    assert out["candidate"] is None
    assert out["playbook_run_executes"] == {"version": 1, "reason": "live version 1"}
    assert out["runs_of_live_since_publish"] == 0
    assert out["versions"] == [dict(out["versions"][0], live=True, candidate=False)]
    assert out["next"] == "playbook_run(name='greeter') executes live version 1."
    # a real run of the live version counts; a candidate test run of v2 does not
    real = await call(env, "playbook_run", name="greeter", inputs='{"greeting": "hi"}',
                      wait_seconds=1)
    assert real["kind"] == "real_run"
    assert await add_candidate(env) == 2
    await call(env, "playbook_run_candidate", name="greeter", inputs='{"name": "x"}',
               wait_seconds=1)
    out = await call(env, "playbook_overview", name="greeter")
    assert out["runs_of_live_since_publish"] == 1
    assert out["candidate"]["version"] == 2
    assert out["candidate"]["last_test_run"]["status"] == "done"
    assert [v["version"] for v in out["versions"]] == [2, 1]
    assert [v["live"] for v in out["versions"]] == [False, True]
    assert [v["candidate"] for v in out["versions"]] == [True, False]
    assert out["next"].startswith("Candidate v2 is not live")


async def test_autonomy_reasons():
    env = await make_env()
    await publish_v1(env, autonomy="manual_only")
    out = await call(env, "playbook_overview", name="greeter")
    assert out["autonomy"] == "manual_only"
    assert out["playbook_run_executes"] == {
        "version": 1, "reason": "live version 1 — manual_only, playbook_run refuses",
    }
    out = await call(env, "playbook_set_autonomy", name="greeter",
                     agent_autonomy="agent_must_confirm")
    assert out["next"] == HINT
    out = await call(env, "playbook_overview", name="greeter")
    assert out["playbook_run_executes"]["reason"] == (
        "live version 1 — runs after the owner approves the per-run card"
    )


class _PendingApprovals(_Approvals):
    def __init__(self):
        super().__init__()
        self.pending: list = []

    async def list_pending(self):
        return list(self.pending)


async def test_parked_run_shape():
    """a parked run is listed as parked (never finished / failed), its
    approval is pending, and the core's pending list is merged by id."""
    approvals = _PendingApprovals()
    env = await make_env(approvals=approvals)
    await publish_v1(env)
    aid = str(uuid.uuid4())
    async with env.sf() as s:
        pb = (await s.execute(select(Playbook).where(Playbook.name == "greeter"))).scalar_one()
        now = datetime.now(timezone.utc)
        parked_on = {"kind": "approval", "approval_id": aid, "since": now.isoformat(),
                     "due_at": (now + timedelta(days=1)).isoformat(), "gate": "run"}
        row = PlaybookRun(playbook_id=pb.id, playbook_version=1, trigger="agent",
                          inputs={}, status="parked", started_at=now, parked_on=parked_on)
        s.add(row)
        await s.commit()
        run_id = str(row.id)
    other_aid = str(uuid.uuid4())
    approvals.pending = [
        SimpleNamespace(id=aid, kind="playbook_run", requested_by_plugin="plugin-playbooks",
                        payload={"playbook": "greeter", "run_id": run_id}),
        # a change card for this playbook, not tied to a run
        SimpleNamespace(id=other_aid, kind="playbook_change",
                        requested_by_plugin="plugin-playbooks", payload={"name": "greeter"}),
        # another playbook's card and another plugin's card: not ours
        SimpleNamespace(id="x", kind="playbook_run", requested_by_plugin="plugin-playbooks",
                        payload={"playbook": "other"}),
        {"id": "y", "kind": "email", "requested_by_plugin": "plugin-mail",
         "payload": {"playbook": "greeter"}},
    ]
    out = await call(env, "playbook_overview", name="greeter")
    assert out["parked_runs"] == [{"run_id": run_id, "parked_on": parked_on}]
    assert out["pending_approvals"] == [
        {"approval_id": aid, "kind": "playbook_run", "run_id": run_id},
        {"approval_id": other_aid, "kind": "playbook_change", "run_id": None},
    ]
    assert out["runs_of_live_since_publish"] == 1  # a real run, parked — counted, not failed
    assert out["next"] == (
        f"playbook_status(run_id='{run_id}') — a parked run has nothing to poll; "
        "it resumes by itself."
    )
    # the row is the source of truth even when the core's list is unavailable
    approvals.pending = []
    out = await call(env, "playbook_overview", name="greeter")
    assert out["pending_approvals"] == [{"approval_id": aid, "kind": "run", "run_id": run_id}]
    st = await call(env, "playbook_status", run_id=run_id)
    assert st["status"] == "parked" and st["kind"] == "real_run"
    assert "next" not in st and HINT in st["hint"]  # parked: nothing to poll


async def test_list_pending_failure_is_not_fatal():
    class _Broken(_Approvals):
        async def list_pending(self):
            raise RuntimeError("core down")

    env = await make_env(approvals=_Broken())
    await publish_v1(env)
    out = await call(env, "playbook_overview", name="greeter")
    assert out["pending_approvals"] == [] and out["playbook"] == "greeter"


async def test_caps_and_more():
    env = await make_env()
    await publish_v1(env)
    now = datetime.now(timezone.utc)
    async with env.sf() as s:
        pb = (await s.execute(select(Playbook).where(Playbook.name == "greeter"))).scalar_one()
        v1 = (await s.execute(select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == pb.id))).scalar_one()
        for i in range(12):
            s.add(PlaybookRun(
                playbook_id=pb.id, playbook_version=1, trigger="agent", inputs={},
                status="parked", started_at=now + timedelta(seconds=i),
                parked_on={"kind": "approval", "approval_id": f"a{i}", "since": "s",
                           "due_at": "d", "gate": "run"},
            ))
        for n in range(2, 13):
            s.add(PlaybookVersion(
                playbook_id=pb.id, version=n, definition=v1.definition, code=v1.code,
                manifest=v1.manifest, author="owner", message=f"v{n}",
                created_at=now + timedelta(seconds=n),
            ))
        await s.commit()
    out = await call(env, "playbook_overview", name="greeter")
    assert len(out["parked_runs"]) == 10 and len(out["pending_approvals"]) == 10
    assert len(out["versions"]) == 10
    assert out["more"] == {"parked_runs": 2, "pending_approvals": 2, "versions": 2}
    # newest first
    assert [p["parked_on"]["approval_id"] for p in out["parked_runs"]][:2] == ["a11", "a10"]
    assert [v["version"] for v in out["versions"]] == list(range(12, 2, -1))
    assert sum(v["live"] for v in out["versions"]) == 0  # v1 is beyond the cap
    assert len(json.dumps(out)) < 6000


async def test_next_hints_point_to_overview():
    """every 'next' site the scope lists carries the overview pointer."""
    env = await make_env()
    out = await call(env, "playbook_propose", name="greeter", code=CODE,
                     agent_autonomy="agent_may_trigger")
    assert out["status"] == "candidate_saved"
    assert out["next"].endswith(HINT) and "playbook_validate" not in out["next"]
    assert "playbook_run_candidate" in out["next"] and "playbook_publish" in out["next"]
    read = parse_read_stage(await env.tools["playbook_edit"](name="greeter"))
    out = await call(env, "playbook_edit", name="greeter", ticket=read["ticket"], code=NEW_CODE)
    assert out["status"] == "candidate_saved" and out["next"].endswith(HINT)
    out = await call(env, "playbook_preflight", name="greeter")
    assert HINT in out["next"], out
    cand = await call(env, "playbook_run_candidate", name="greeter", inputs='{"name": "x"}',
                      wait_seconds=1)
    assert cand["next"] == HINT
    out = await call(env, "playbook_publish", name="greeter", explanation=EXPLANATION)
    assert out["status"] == "published" and out["next"] == HINT
    out = await call(env, "playbook_set_autonomy", name="greeter", agent_autonomy="manual_only")
    assert out["next"] == HINT
    await call(env, "playbook_set_autonomy", name="greeter", agent_autonomy="agent_may_trigger")
    env.runner.status = "failed"
    real = await call(env, "playbook_run", name="greeter", inputs='{"name": "x"}', wait_seconds=1)
    assert real["status"] == "failed" and real["next"] == HINT
    st = await call(env, "playbook_status", run_id=real["run_id"])
    assert st["status"] == "failed" and st["next"] == HINT
    # the failed hint: overview pointer AFTER the stubs_from_run sentence
    hint = st["hint"]
    assert "stubs_from_run" in hint
    assert hint.index("stubs_from_run") < hint.index("playbook_overview(")
    env.runner.status = "running"
    running = await call(env, "playbook_run", name="greeter", inputs='{"name": "x"}',
                         wait_seconds=0)
    assert running["status"] == "running" and HINT in running["message"]
    st = await call(env, "playbook_status", run_id=running["run_id"])
    assert st["status"] == "running" and "next" not in st and HINT in st["hint"]
    runs = await call(env, "playbook_runs", name="greeter")
    assert runs["next"] == HINT
    ov = await call(env, "playbook_overview", name="greeter")
    assert ov["runs_of_live_since_publish"] == 2 and ov["versions"][0]["live"] is True
    assert "playbook_overview" not in ov["next"]


async def test_dry_run_and_refusals_do_not_hint_as_runs():
    env = await make_env()
    await live_with_candidate(env)
    out = await call(env, "playbook_dry_run", name="greeter", inputs='{"name": "x"}')
    assert out["kind"] == "dry_run" and out["status"] == "simulated"
    out = await call(env, "playbook_run", name="nope")
    assert list(out) == ["error"]


def test_overview_is_read_only_and_ungated():
    tds = {td.name: td for td, _ in build_tools(None, _Bus(), _StubRunner())}
    td = tds["playbook_overview"]
    assert td.modes == ["planning", "building"]
    assert td.policy == "auto_approve" and td.risk_level == "low"
    assert td.parameters == {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Playbook name"}},
        "required": ["name"],
    }
    assert getattr(td, "timeout_seconds", None) is None
    assert getattr(td, "chat_only", False) is False
    assert "truth surface" in td.description and "Read-only" in td.description
    # not skill-gated: luna's SkillDef contract lists gated tools only
    assert "playbook_overview" not in PlaybooksPlugin.AUTHORING_TOOLS
    assert "playbook_overview" not in PlaybooksPlugin.DELEGATION_TOOLS
    for skill in PlaybooksPlugin.manifest.skills:
        assert "playbook_overview" not in skill.tools, skill.name
        assert "playbook_overview" in skill.body or skill.name == "playbook-delegation"
    assert "playbook_overview" in tds


async def test_overview_writes_nothing():
    env = await make_env()
    await live_with_candidate(env)

    async def snapshot():
        async with env.sf() as s:
            pb = (await s.execute(select(Playbook).where(Playbook.name == "greeter"))).scalar_one()
            runs = (await s.execute(select(PlaybookRun.id))).all()
            vers = (await s.execute(select(PlaybookVersion.version))).all()
            return (pb.live_version, pb.candidate_version, sorted(map(str, (r[0] for r in runs))),
                    sorted(v[0] for v in vers))

    before = await snapshot()
    started, requests = len(env.runner.started), len(env.approvals.requests)
    for _ in range(3):
        await call(env, "playbook_overview", name="greeter")
    assert await snapshot() == before
    assert len(env.runner.started) == started and len(env.approvals.requests) == requests
