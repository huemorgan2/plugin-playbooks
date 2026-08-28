# 012 / phase 4 — real shapes first

Status: READY (updated 2026-08-28 from phase-3 learnings; baseline
0.21.0 `3d07f8a`, 190 tests green).

## Learnings carried in from phases 2–3

- The skill body is byte-budgeted: `test_payload_diet_budgets` pins it
  at ≤12,288B and it sits at 12,258B. The new skill prose line (scope
  item 3) must fit — trim elsewhere in the body, never raise the budget.
- Re-sync git + the three version stamps immediately before bumping
  (concurrent session ships in this repo).
- CDP tenant verify: open a fresh tab (PUT /json/new + Page.navigate
  over its websocket) if no luna.com.ai tab exists; proxy-login returns
  `access_token` (not `token`).
- expire_on_commit: capture ORM attribute values before `commit()`.

## Scope (from PLAN.md, pre-phase-2 draft)

1. `playbook_status` on a **failed** run appends a steering hint: capture
   this run's REAL tool outputs as spec stubs via
   `playbook_spec_from_run(name=..., run_id=<this run>)`.
2. `playbook_spec_from_run` accepts failed runs — stubs pin every step
   that DID run; expectations default to the failure point. Today it
   auto-picks only `done` runs.
3. Skill prose: "write stubs from recorded reality, not memory — after
   any real run exists, start specs with playbook_spec_from_run."

## Coordination note

plans/014-failed-run-awareness (separate, uncommitted as of 2026-08-28)
adds a failing-playbooks digest via prompt_sections. No file overlap
expected (014: model column + failures.py + prompt section; here:
agent_tools spec_from_run/_status + skill body), but re-check at
execution time — 014 may have landed changes to `_status` output.

## Verification

- Unit tests: failed-run pinning (stubs stop at failure point,
  expectations from failure), hint presence on failed `playbook_status`.
- Full suite green; real-Luna probe of spec_from_run on a failed run.

## Ship

Minor bump, push, publish, tenant upgrade + verify. Update PLAN.md
status to EXECUTED and write the plan-level closing note.
