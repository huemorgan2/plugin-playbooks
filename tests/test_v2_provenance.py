"""plans/032 phase 09 — the result provenance envelope (master §2 "Result
provenance"): every run-shaped result opens with
`kind, side_effects, version, version_role, run_id`; a dry run says
`simulated`, never `done`; refusals carry no envelope."""

from __future__ import annotations

import json
import uuid

import pytest
from _provenance_env import call, live_with_candidate, make_env, publish_v1
from evidence import EXPLANATION, green_run
from sqlalchemy import select
from test_manifest_drift import _Bus, _StubRunner

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import PlaybookRun
from plugin_playbooks.provenance import (
    ENVELOPE_KEYS,
    KINDS,
    ROLES,
    envelope,
    row_provenance,
    with_envelope,
)

FIVE = ["kind", "side_effects", "version", "version_role", "run_id"]
RUN_SHAPED = (
    "playbook_run", "playbook_run_candidate", "playbook_dry_run",
    "playbook_status", "playbook_runs",
)


def test_with_envelope_key_order():
    env = envelope("real_run", side_effects=True, version=3, version_role="live", run_id="r")
    assert list(env) == FIVE == list(ENVELOPE_KEYS)
    out = with_envelope(env, {"b": 1, "a": 2})
    assert list(out) == FIVE + ["b", "a"]
    # a repeated key keeps the envelope's slot and takes the body's value
    out = with_envelope(env, {"run_id": "x", "z": 0})
    assert list(out) == FIVE + ["z"] and out["run_id"] == "x"
    with pytest.raises(ValueError):
        envelope("done", side_effects=True, version=1, version_role="live", run_id="r")
    with pytest.raises(ValueError):
        envelope("dry_run", side_effects=False, version=1, version_role="old", run_id=None)
    assert KINDS == ("real_run", "candidate_test_run", "dry_run")
    assert ROLES == ("live", "candidate", "historical")
    assert row_provenance(is_test=True, trigger="agent") == ("candidate_test_run", "candidate")
    assert row_provenance(is_test=False, trigger="agent-candidate") == ("candidate_test_run", "candidate")
    assert row_provenance(is_test=False, trigger="schedule") == ("real_run", "live")


async def _live_v1_candidate_v2_with_runs():
    env = await make_env()
    await live_with_candidate(env)
    real = await call(env, "playbook_run", name="greeter", inputs='{"greeting": "hi"}', wait_seconds=1)
    cand = await call(env, "playbook_run_candidate", name="greeter", inputs='{"name": "x"}', wait_seconds=1)
    return env, real, cand


@pytest.mark.parametrize("tool", RUN_SHAPED)
async def test_envelope_first_in_key_order(tool):
    env, real, cand = await _live_v1_candidate_v2_with_runs()
    if tool == "playbook_run":
        out = real
    elif tool == "playbook_run_candidate":
        out = cand
    elif tool == "playbook_dry_run":
        out = await call(env, "playbook_dry_run", name="greeter", inputs='{"name": "x"}')
    elif tool == "playbook_status":
        out = await call(env, "playbook_status", run_id=real["run_id"])
    else:
        out = (await call(env, "playbook_runs", name="greeter"))["runs"][0]
    assert list(out)[:5] == FIVE, list(out)


async def test_playbook_run_is_real_run_of_live():
    env, out, _ = await _live_v1_candidate_v2_with_runs()
    row = env.runner.started[0]
    async with env.sf() as s:
        rows = (await s.execute(
            select(PlaybookRun).where(PlaybookRun.is_test.is_(False))
        )).scalars().all()
    assert len(rows) == 1 and rows[0].trigger == "agent"
    assert out["kind"] == "real_run" and out["side_effects"] is True
    assert out["version"] == 1 and out["version_role"] == "live"
    assert out["run_id"] == str(rows[0].id)
    assert out["status"] == "done"
    assert "un-promoted candidate (v2)" in out["note"]  # the candidate note stays
    assert "playbook_overview(name='greeter')" in out["next"]
    assert row[0].name == "greeter"


async def test_run_candidate_is_candidate_test_run():
    env, _, out = await _live_v1_candidate_v2_with_runs()
    assert out["kind"] == "candidate_test_run" and out["side_effects"] is True
    assert out["version"] == 2 and out["version_role"] == "candidate"
    assert out["candidate_version"] == 2  # the pre-envelope key stays
    async with env.sf() as s:
        row = await s.get(PlaybookRun, uuid.UUID(out["run_id"]))
    assert row.is_test is True and row.playbook_version == 2
    assert "playbook_overview(" in out["next"]


async def test_dry_run_is_simulated_not_done():
    env = await make_env()
    await live_with_candidate(env)
    # v1 stub answers `done` — the tool boundary says `simulated`
    out = await call(env, "playbook_dry_run", name="greeter", inputs='{"name": "x"}')
    assert list(out)[:5] == FIVE
    assert out["status"] == "simulated" and out["dry_run"] is True and out["banner"]
    assert out["kind"] == "dry_run" and out["side_effects"] is False and out["run_id"] is None
    assert out["version"] == 2 and out["version_role"] == "candidate"  # auto → candidate
    out = await call(env, "playbook_dry_run", name="greeter", version="live", inputs='{"greeting": "x"}')
    assert out["version"] == 1 and out["version_role"] == "live"
    # promote v2, then dry-run the explicit old number → historical
    await call(env, "playbook_publish", name="greeter", explanation=EXPLANATION)
    out = await call(env, "playbook_dry_run", name="greeter", inputs='{"name": "x"}')
    assert out["version"] == 2 and out["version_role"] == "live"  # no candidate now
    out = await call(env, "playbook_dry_run", name="greeter", version="1", inputs='{"greeting": "x"}')
    assert out["version"] == 1 and out["version_role"] == "historical"
    # a failed simulation stays failed
    env.runner.dry_status = "failed"
    out = await call(env, "playbook_dry_run", name="greeter", inputs='{"name": "x"}')
    assert out["status"] == "failed" and out["kind"] == "dry_run"


async def test_dry_run_python_keeps_simulated():
    env = await make_env()
    code = "async def run(ctx, inputs):\n    return {'ok': True}\n"
    out = await call(env, "playbook_propose", name="py", code=code, format="python")
    assert out["status"] == "candidate_saved", out
    out = await call(env, "playbook_dry_run", name="py", inputs="{}")
    assert list(out)[:5] == FIVE
    assert out["status"] == "simulated" and out["kind"] == "dry_run"
    assert out["version"] == 1 and out["version_role"] == "candidate"


async def test_status_and_runs_derive_from_the_row():
    env, real, cand = await _live_v1_candidate_v2_with_runs()
    st = await call(env, "playbook_status", run_id=real["run_id"])
    assert list(st)[:5] == FIVE
    assert st["kind"] == "real_run" and st["version"] == 1 and st["version_role"] == "live"
    assert st["side_effects"] is True and st["run_id"] == real["run_id"]
    assert st["playbook"] == "greeter" and st["status"] == "done"
    assert "playbook_overview(name='greeter')" in st["next"]
    st = await call(env, "playbook_status", run_id=cand["run_id"])
    assert st["kind"] == "candidate_test_run" and st["version"] == 2
    assert st["version_role"] == "candidate"
    # promote v2: the old candidate run is still a candidate test run
    await green_run(env.sf, 2)
    out = await call(env, "playbook_publish", name="greeter", explanation=EXPLANATION)
    assert out["status"] == "published" and out["live_version"] == 2, out
    assert "playbook_overview(" in out["next"]
    st = await call(env, "playbook_status", run_id=cand["run_id"])
    assert st["kind"] == "candidate_test_run" and st["version_role"] == "candidate"
    runs = await call(env, "playbook_runs", name="greeter")
    assert list(runs)[0] == "playbook"
    assert "kind" not in runs  # the list is not a run
    by_id = {r["run_id"]: r for r in runs["runs"]}
    for r in runs["runs"]:
        assert list(r)[:5] == FIVE
    assert by_id[real["run_id"]]["kind"] == "real_run"
    assert by_id[real["run_id"]]["version_role"] == "live"
    assert by_id[cand["run_id"]]["kind"] == "candidate_test_run"
    assert by_id[cand["run_id"]]["version_role"] == "candidate"
    assert by_id[cand["run_id"]]["is_test"] is True and by_id[cand["run_id"]]["version"] == 2
    assert "playbook_overview(" in runs["next"]


async def test_refusals_carry_no_envelope():
    env = await make_env()
    await publish_v1(env)
    out = await call(env, "playbook_set_autonomy", name="greeter", agent_autonomy="manual_only")
    assert out["status"] == "updated" and "playbook_overview(" in out["next"]
    out = await call(env, "playbook_run", name="greeter", inputs='{"greeting": "x"}')
    assert out["status"] == "refused" and out["playbook"] == "greeter"
    assert "manual_only" in out["reason"] and "kind" not in out
    assert list(out) == ["status", "playbook", "reason"]
    out = await call(env, "playbook_propose", name="solo", code=(
        "playbook(name='solo', description='d')\n"
        "say = tool('send_chat_message', message=inputs.greeting)\n"
    ))
    assert out["status"] == "candidate_saved"
    out = await call(env, "playbook_run", name="solo", inputs='{"greeting": "x"}')
    assert "no live version" in out["error"] and out["runnable_via"] == "playbook_run_candidate"
    assert "kind" not in out
    out = await call(env, "playbook_run", name="nope")
    assert list(out) == ["error"]
    assert env.runner.started == []  # no row for any refusal


def test_descriptions_name_the_envelope():
    tds = {td.name: td for td, _ in build_tools(None, _Bus(), _StubRunner())}
    for name in RUN_SHAPED:
        d = tds[name].description
        assert "kind" in d and "version_role" in d, name
    assert "SIMULATED" in tds["playbook_dry_run"].description


async def test_parked_run_result_keeps_its_keys_after_the_envelope():
    """plugin/08's parked result `{run_id, playbook, status, approval_id,
    message}` sits after the five keys (dojop/02 keys on status == parked)."""
    env = await make_env()
    await publish_v1(env, autonomy="agent_must_confirm")
    aid = str(uuid.uuid4())

    async def parked_start(playbook, inputs=None, trigger=None, is_test=False, **kw):
        assert kw.get("needs_owner_card") is True
        env.runner.status = "parked"
        row = await _RowRunnerStart(env.runner)(playbook, inputs=inputs, trigger=trigger, is_test=is_test)
        async with env.sf() as s:
            r = await s.get(PlaybookRun, row.id)
            r.parked_on = {"kind": "approval", "approval_id": aid, "since": "s", "due_at": "d", "gate": "run"}
            await s.commit()
            await s.refresh(r)
        return r

    env.runner.start_run_background = parked_start
    out = json.loads(await env.tools["playbook_run"](name="greeter", inputs='{"greeting": "x"}'))
    assert list(out) == FIVE + ["playbook", "status", "approval_id", "message", "next"]
    assert out["status"] == "parked" and out["approval_id"] == aid
    assert out["kind"] == "real_run" and out["version"] == 1 and out["version_role"] == "live"
    assert out["message"].startswith(f"run {out['run_id']} waiting on owner card #{aid}")
    assert "playbook_overview(" in out["next"]
    async with env.sf() as s:
        row = await s.get(PlaybookRun, uuid.UUID(out["run_id"]))
    assert row.wake_on_complete is True


class _RowRunnerStart:
    """The stub's original `start_run_background`, bound (see above)."""

    def __init__(self, runner) -> None:
        self._r = runner

    async def __call__(self, playbook, **kw):
        return await type(self._r).start_run_background(self._r, playbook, **kw)
