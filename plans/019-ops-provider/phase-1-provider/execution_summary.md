# Phase 1 — provider: execution summary

**Shipped: plugin-playbooks 0.31.0, published to the official marketplace,
upgraded and verified on QA Luna (port 8767).** Commit `7fa9fe3`
(huemorgan2/plugin-playbooks). 343 tests green (321 baseline + 22 new),
zero regressions.

## What shipped

- `plugin_playbooks/ops_provider.py` (new) — `ops_authority` (provider
  registry lookup, key "ops", degrades to None on old cores / broken
  registries), `report_problem` (`ops.problem_reported` emit; returns False
  when the emit is impossible so the caller falls back), `scope_refusal`
  (tool-layer plan-scope gate: only kind-`ops` conversations with an active
  `plan_only` plan; undeclared `playbook:<name>` → refusal JSON with
  `gate: ops_plan_scope` and a steering hint toward `ops_file_plan`),
  `report_outcome` (`ops.outcome` emit, kind-`ops` only, never raises).
- `fix_proposals.py` — with plugin-ops present, live-run failures (new AND
  repeats) are reported to the ops ledger: no fix-proposal card, no state
  flip, no wake from playbooks. The `playbook_fix_proposals` ledger row is
  still written (the failure digest reads it). Without plugin-ops the 0.30.x
  card path is unchanged; the fallback is logged once per process.
- `agent_tools.py` — scope gate at the top of every mutation handler:
  `playbook_propose`, `_edit_impl` (edit + edit_force), `playbook_manifest_set`,
  `_do_publish` (publish + rollback), `playbook_spec_add`,
  `playbook_spec_delete`. `_do_publish`'s success tail emits `ops.outcome`
  right after `announce_publish` (facts: action, playbook, old/new live
  versions, evidence run id, gate results, change summary).
- `tests/test_ops_provider.py` — 22 tests: authority lookup, problem emit
  (payload shape, 400-char error truncation, no-ops → False, broken bus →
  False), scope matrix (non-ops chat / no ops / no plan / anything_needed /
  declared / undeclared / broken query fails open), tool-layer gating via
  `build_tools`, outcome gating (ops chat yes, building chat no, broken bus
  never raises), and fix_proposals routing against a real sqlite DB (report
  + ledger, repeat + count bump, degrade → card path).

## Verified how

- Full suite: 343 passed against the luna venv.
- Real QA Luna 8767: marketplace upgrade 0.30.3 → 0.31.0 via the upgrade
  route (not the disable/enable toggle — phase-1/plugin-ops learning).
  Created a deliberately failing live playbook `qa-ops-probe` (code step,
  goes live at version 1) and ran it twice through the real runner:
  - Run 1 (failed) → `ops_problems` row `provider=plugin-playbooks`,
    `area_ref=playbook:qa-ops-probe`, status `open` — created by plugin-ops
    from the bus event, proving the full live path (runner →
    `playbook.run.completed` → FixProposalService → `ops.problem_reported`
    → plugin-ops ledger).
  - Run 2 (failed, same signature) → `times_seen` bumped to 2, still ONE row.
  - Zero `playbook_fix_proposal` approval cards filed — the 0.30.x card path
    correctly stood down with plugin-ops installed.
- Scope refusal was verified at the unit/tool layer only; the live
  ops-chat exercise (agent hitting an out-of-plan target and being refused)
  is deferred to phase 3 E2E, as PHASE.md allowed.

## Deviations from PHASE.md

None. One incidental find, not a defect: `qa-ops-probe`'s failure error on QA
is "code steps need plugin-inline-code-run installed" (that plugin isn't on
QA) — the failure fired at the intended step either way, and any failed live
step exercises the same path.

## Surprises / learnings

- The provider path needed no state writes at all — the `identify →
  fix_publish` flip now lives exclusively inside the degrade card path, so
  the state-residue problem from plugin-ops phase 1 (its bug 1) cannot recur
  from 0.31.0 playbooks.
- `report_problem` returning False on a broken bus (not just on missing ops)
  matters: it reroutes the failure to the card path instead of losing it.

## QA residue for phase 3

- `qa-ops-probe` playbook (always fails) + its open ops problem — usable as
  a live problem source in phase 3; purge both when done.
- `qa-sig-002` seeded problem with its pending `ops_fix_plan` approval card —
  phase 3's negotiation/approval loop can start from it.
- Throwaway conversation `b3f7996f-cebc-4c01-963a-c6681e7168c4` (qa-toolcheck).

## Reassessment of remaining phases

- **Phase 3 (E2E on QA with real browser):** unchanged in scope, two
  additions from this phase: (1) the scope-refusal live check must target
  `qa-ops-probe` or another undeclared playbook while a `plan_only` plan is
  executing — the unit layer is covered, only the live agent behavior
  remains; (2) if the agent should actually FIX something end-to-end, give
  the plan a playbook whose failure is repairable from playbook tools alone
  (`qa-ops-probe`'s missing-plugin failure is not) — seed one with a bad
  template/step instead. Also upgrade plugin-chat-ui on QA to 0.24.0 before
  screenshots (QA still shows 0.23.0).
