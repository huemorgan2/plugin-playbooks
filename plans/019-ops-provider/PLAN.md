# 019 — Playbooks becomes an ops provider

**Status: APPROVED (2026-09-01) — executing (phased execution).**

Part of luna-plugins master plan 013. The generic machinery (plan
object, negotiation, disk record, ops chat ownership) moved to the new
plugin-ops (`plugins/plugin-ops/plans/001-ops-foundation`). This plan
is the thin playbooks side: playbooks is one provider of fixable area —
the first, not the only.

## What playbooks stops doing

- Raising `playbook_fix_proposal` approval cards (the premature
  "approve a fix attempt" card) — replaced by reporting to plugin-ops.
  Pending cards at upgrade decide as before; code path kept one
  release, deprecated.
- Owning the ops conversation (`ops_conversation_id` and the
  state-flip-on-approval logic move to plugin-ops).

## What playbooks does as a provider

1. **Report problems.** On a live-run failure, emit
   `ops.problem_reported` {provider: "plugin-playbooks", area_ref:
   "playbook:<name>", signature (existing dedupe signature), evidence
   (run id, version, step, error), display (name, purpose)}. The
   existing failure-detection plumbing stays; only the card-raising is
   replaced.
2. **Provide the fix tools.** The existing playbook tools remain the
   way fixes happen, mode-gated exactly as today (identify = read-only
   set; fix_publish = full set; `tools="all"` cannot override modes).
3. **Enforce plan scope in the tool layer.** Mutation tools
   (candidate edit, publish, rollback) consult plugin-ops' active-plan
   query: under a `plan_only` approval they refuse any target the
   approved plan did not declare, with a steering hint ("outside the
   approved plan — file a revised plan"). Under `anything_needed`, no
   target restriction. Tool-layer gate, never prompt discipline.
4. **Report outcomes.** On a successful gated publish, emit
   `ops.outcome` with the facts plugin-ops renders into
   execution_summary.md: live version before/after, candidate run ids
   and statuses, gates passed, diff summary, announce id. Rollback
   publishes the same way.
5. **Publish autonomy unchanged.** In `fix_publish` with
   `publish_autonomy='auto'`, publish records an audited auto-approval
   (existing 0.30.x path) — the plan approval is the consent.
   `publish_autonomy='ask'` per playbook remains the opt-out for a slim
   final card.

## Degrade

Without plugin-ops installed: fall back to current 0.30.x behavior
(fix-proposal cards) and say so once in the log. With plugin-ops but an
old core (< luna 096 card UI): the plan card renders with plain
approve/reject — plain approve = `plan_only`.

## Tests

- Failure → `ops.problem_reported` emitted with correct
  signature/evidence; no card raised; dedupe unchanged.
- Scope enforcement matrix: plan_only + declared target (allowed),
  plan_only + undeclared target (refused + hint), anything_needed
  (allowed), no active plan (refused in fix modes? — no: outside an
  ops execution the tools behave as today; enforcement applies only
  when an active plan exists for the conversation).
- Publish success → `ops.outcome` facts complete.
- Fallback path without plugin-ops.
- Full plugin pytest suite, zero regressions vs 0.30.3 baseline.

## Version

0.31.0 (three stamps). Publish after push per standing rule.
