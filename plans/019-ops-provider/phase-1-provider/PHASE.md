# Phase 1 — playbooks as an ops provider (whole plan, one phase)

Baseline before changes: **321 tests passed** at 0.30.3 (`e9f2481`).

## Scope

Everything in PLAN.md, informed by plugin-ops phase 1 (plans/001) learnings:

1. New `plugin_playbooks/ops_provider.py`: presence check for the "ops"
   provider (via `ctx.provider_registry`), `ops.problem_reported` emit,
   tool-layer scope refusal (active_plan query), `ops.outcome` emit.
2. `fix_proposals.py`: when plugin-ops is present, live-run failures emit
   `ops.problem_reported` (new problems AND repeats — plugin-ops owns the
   counter and evidence refresh) and never raise a card, never flip state,
   never wake. The ledger row is still recorded (the failure digest reads
   it). Without plugin-ops: the 0.30.x card path unchanged, one log line
   naming the fallback.
3. Scope enforcement at the top of the mutation handlers (`playbook_propose`,
   `_edit_impl` — covers edit and edit_force —, `playbook_manifest_set`,
   `_do_publish` — covers publish and rollback —, `playbook_spec_add`,
   `playbook_spec_delete`): only in a kind-`ops` conversation with an
   active plan; `plan_only` + undeclared `playbook:<name>` target → refusal
   with a steering hint; `anything_needed` or no active plan → unchanged.
4. `_do_publish` success tail: emit `ops.outcome` (facts: action, versions,
   evidence run, gates, summary) — only from a kind-`ops` conversation with
   plugin-ops present, so a routine building-chat publish never closes an
   executing plan via the single-executing fallback match.
5. State ownership: playbooks no longer sets ops-chat state on the ops path
   (the `identify → fix_publish` flip stays only inside the degrade card
   path). plugin-ops owns convergence (its phase-1 bug 1 came from our
   residue).

## Deliverables

- `plugin_playbooks/ops_provider.py` + changes above.
- `tests/test_ops_provider.py`: problem-report emit (new + repeat, no card),
  degrade path (no ops provider → card path), scope matrix (plan_only
  declared / plan_only undeclared / anything_needed / no plan / non-ops
  conversation), outcome emit on publish success (and not from a building
  chat).
- Version 0.31.0 in all three stamps; commit, push, publish to official.

## Verification

- Full suite green, zero regressions vs the 321 baseline.
- Real QA Luna 8767: upgrade plugin-playbooks to 0.31.0, plugin-ops present;
  a seeded live-run failure produces an ops problem row (not a
  playbook_fix_proposal card); scope refusal exercised via the ops chat if
  reachable in this phase, else deferred to phase 3 E2E (noted in summary).
