# 012 / phase 4 — execution summary

Shipped as **0.23.0**, commit `fff7d71`, pushed to
huemorgan2/plugin-playbooks main, published to marketplaces.com.ai
(catalog latest_version 0.23.0), and upgraded on the live tenant
vaselin-scanny-2 (0.21.0 → 0.23.0, hot-loaded: enabled, active, 24
tools, no restart). Baseline was 0.22.0 / `6e9d85b` — the concurrent
session's plans/014 (failed-run awareness) landed between phases 3 and
4; no file-level conflicts (014: playbook_ack_failures + digest; here:
_status hint, _spec_from_run, specs.spec_from_run, skill bullet).

## What changed

- `plugin_playbooks/agent_tools.py`
  - `_status`: a run with status `failed` now carries a `hint` steering
    the agent to pin the run's REAL recorded outputs before fixing:
    `playbook_spec_from_run(name='<playbook>', run_id='<this run>')`.
    (Previously only `running` had a hint.)
  - `_spec_from_run`: auto-pick prefers the latest `done` run and now
    falls back to the latest `failed` one; a run selected by `run_id=`
    must be finished (`done`/`failed`) — `running`/`cancelled` are
    rejected with a named reason. The no-runs error reads "No finished
    run" instead of "No completed run". The `next` note is
    failure-aware: for a failed run it says the stubs are the value,
    the expect block documents current behavior, and expect should be
    updated after the fix.
  - `playbook_spec_from_run` ToolDef description now says failed runs
    work and describes the fallback.
- `plugin_playbooks/specs.py` — `spec_from_run` on a non-done run seeds
  `expect.error_contains` from the last failing step's error (truncated
  to 120 chars; error_contains is a substring match). Docstring updated.
- `plugin_playbooks/__init__.py` — SPECS skill bullet swapped at equal
  size: "Write stubs from recorded reality, not memory: after ANY real
  run — even a FAILED one — start from playbook_spec_from_run(name);
  trim, then save." Skill body 12,259B (budget 12,288, was 12,258).
- `tests/test_specs.py` — four new tests: failed-run auto-pick pins the
  failure (stubs stop at the failing step, error_contains matches the
  status error, proposal round-trips through playbook_spec_add and
  passes), done-preferred-over-newer-failed (and the failed run stays
  reachable via run_id), unfinished-run rejection (inserted `running`
  row), and hint presence on failed `playbook_status`.

## Verification

- Full suite: **211 passed** (207 merged baseline + 4 new), zero
  regressions.
- Byte budget test green: skill body 12,259B ≤ 12,288.
- Production: publish → catalog 0.23.0 → tenant upgrade → `/api/plugins`
  shows 0.23.0 active, 24 tools (playbook_spec_from_run present), no
  restart.

## Deviations from PHASE.md

- "Real-Luna probe of spec_from_run on a failed run" downgraded to the
  version/tool-list tenant check: the changed paths are tool handlers
  with no HTTP route, so a real probe means driving an agent turn —
  live-tenant turns are owner-visible and QA :8766 belongs to the other
  session (same pre-declared skip as phases 2–3). The round-trip is
  covered by the new spec_add round-trip test instead.

## Surprises / learnings

- None structural. The failed-run round-trip works because
  `spec_from_run` already emitted `expect.status: failed`; phase 4 only
  had to open the selection gate and seed the error expectation.

## Reassessment of remaining phases

- None remain. Phases 2–4 executed (phase 1 shipped earlier as 0.17.0).
  PLAN.md marked EXECUTED with the closing note.
