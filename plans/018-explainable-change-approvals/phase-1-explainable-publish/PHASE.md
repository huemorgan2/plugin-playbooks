# Phase 1 — publish becomes the aggregate, explainable approval

## Scope

`plugin_playbooks/agent_tools.py` (`playbook_publish` / `playbook_rollback`),
new helper(s) in the same file. No route changes — the owner's UI promote
path (`routes.py promote_version`) stays as-is (the owner needs no approval
card for their own click).

1. **`explanation` argument** — required on `playbook_publish` and
   `playbook_rollback`. Handler-enforced (not schema-only): missing or
   < 80 characters → refusal with a steering hint telling the agent to write
   2–6 owner-language sentences (what was broken / what this changes / why
   it is safe). Flows live in the tool layer.
2. **Policy flip** — both tools go `prompt_always` → `auto_approve`. The
   handler runs the five gates FIRST (static validation, specs, test run,
   drift, probes), then raises ONE `ctx.approval.request(kind=
   "playbook_change", ...)` itself:
   - `payload={"name", "version": <target row>, "action": "publish"|"rollback"}`
     — payload identity only; presentation stays advisory (luna 094).
   - `presentation`: eyebrow `PLAYBOOK CHANGE`; headline = first line of the
     explanation clipped to 90 chars; explanation = agent text + one evidence
     line ("Tests green: run <id> of version <v>." or prior-live-history for
     restores); `changes[]` = code diff live→target (both already in hand),
     manifest diff when changed, spec-set change note when the spec names
     differ between live and target version.
   - `conversation_id` = ops chat (fallback None → engine routes by its own
     fallback).
   - `summary` stays a one-line fallback for old UIs.
3. **Lock discipline** — today the whole publish runs under one
   `with_for_update()` session. An owner decision can take hours, so the flow
   splits: session A (gates + material gathering + commit of spec/probe
   caches) → await decision, no DB lock held → session B re-locks, re-checks
   the target is still what was approved (candidate pointer unchanged /
   version still not live), flips live. Stale target after approval →
   refusal telling the agent to re-publish.
4. **`publish_autonomy="auto"`** — honored for real now (the model has
   documented it since 0.26.0 but nothing consulted it): in the ops chat's
   `fix_publish` state an auto playbook records `record_auto_approval(...)`
   (with the same presentation, so History reads well) and proceeds without
   blocking.
5. **Rejected** → tool returns a refusal with the owner's reason, telling
   the agent to relay it and stand down.
6. **Degrade** — no ctx / no approval engine (unit tests, headless cores):
   publish proceeds as today (the old core-gate prompt also didn't exist in
   those contexts); logged.

## Verification

- New tests (tests/): explanation gate (missing/short → steering refusal, no
  DB mutation); approved path flips live and files exactly one
  `playbook_change` approval with the presentation shape (fake approvals
  object); rejected path leaves live untouched and returns the owner reason;
  stale-candidate-after-approval refusal; auto-publish autonomy path records
  an auto approval; rollback carries explanation + diff; degrade path (no
  ctx) still publishes.
- Full plugin suite green (baseline 307).
- Live-browser verification deferred to the master plan's phase 4 E2E
  (combined for all three 018 phases), per plan.

## Ship

Batched with phases 2–3 into 0.30.0 (single version; nothing user-visible
ships alone from this phase).
