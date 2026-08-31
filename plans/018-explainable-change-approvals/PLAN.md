# 018 — Explainable change approvals

**Parent:** luna-plugins `plans/012-readable-ops-approvals/PLAN.md`
**Depends on:** luna core ≥ 0.91.000 (plan 094 — `presentation` on approvals; moment-turn state fix).
**Version:** `0.30.0` — bump ALL THREE stamps (`__init__.py` PluginManifest, `luna-plugin.toml`, `pyproject.toml`).

## Purpose

Playbook-change approvals in ops must read as: **what issue was found → what the fix does (plain English) → one approval for the whole fix → technical diff collapsed behind a toggle**, and they must arrive as reactions to failures the agent itself surfaced. Today the cards are docstring + raw kwargs, one card per gated call, and the fix-proposal loop has three bugs that mute or cripple it.

## Result

- A failing live run files ONE readable fix-proposal card in ops (plain-English story, raw error behind the fold).
- Approving it wakes the agent **with tools**; it fixes the candidate and requests publish.
- The publish approval is ONE card for the whole fix: agent-authored explanation + full live→candidate diff (code + manifest) + test evidence, collapsed by default.
- Rollback and the other gated mutations carry explanations the same way.

---

## Phase 1 — publish becomes the aggregate, explainable approval

- `playbook_publish` (`agent_tools.py`) gains a required `explanation` arg: 2–6 sentences of owner-language ("what was broken / what this changes / why it is safe"), enforced in the handler (refuse with a steering hint when missing/too short/jargon-coded — flows live in the tool layer, not prose).
- Policy `prompt_always` → `auto_approve`; the handler runs the five publish gates FIRST (validation, specs, test-run, drift, probes), then raises ONE `ctx.approval.request(kind="playbook_change", conversation_id=ops-or-origin, risk_level="medium", payload={"name", "version", "action": "publish"}, presentation={...})`:
  - eyebrow `PLAYBOOK CHANGE`, headline from the explanation's first line (≤90 chars), explanation = agent text + one evidence line ("Tests green: run <n> of version <v>").
  - `changes[]`: code diff live→candidate (both sides already in hand — candidate row code vs. `_derive_code(live)`), manifest diff when changed, spec changes when any.
- Rejected → tool returns a refusal telling the agent to relay the owner's reason and stand down. Approved → flip live + existing `announce_publish`.
- `playbook_rollback` same pattern (`explanation` arg, diff live→previous version).
- Dedup/supersede identity stays payload-only; verify a re-request after an edit supersedes the stale card.

Tests: explanation gate (missing/short), one card per publish, presentation shape, diff correctness, reject path, grants still apply.

## Phase 2 — fix-proposal loop: readable + actually working

- `fix_proposals.py`:
  - Card gets `presentation`: eyebrow `PLAYBOOK FAILING`, headline `"'{name}' is failing — approve a fix attempt"`, explanation = plain-English: what the playbook does (first manifest line), which step failed, how many times, what approving does. Raw run id / version / error[:400] move into `changes[]` (`kind:"text"`) behind the fold. `summary` stays as a one-line fallback for old UIs.
  - **Bug:** `getattr(ctx, "approval", None)` — `ctx.approval` raises `RuntimeError` when unwired, and the outer `except` silently eats the card. Use try/except around property access; fall back to the documented ledger-only path.
  - **Bug:** the approved-fix wake `send_muted_message(..., channel="moment")` passes no `tools` → tool-free turn that cannot diagnose anything. Pass `tools="all"` (match plugin_tasks) and rely on core state gating (fixed in 094 phase 3) to scope it.
- Tests: presentation on the card, RuntimeError fallback, wake message carries tools; extend `test_build_operate.py` with a fake ctx exercising the full card path (today only the ledger path is unit-tested).

## Phase 3 — remaining gated mutations + hygiene

- `playbook_edit_force`, `playbook_manifest_set`, `playbook_spec_delete`, `playbook_set_autonomy`: add optional-but-steered `why` arg; where they stay `prompt_always`, put `why` first in args so the legacy card leads with it; `manifest_set` shows a manifest diff via presentation if converted handler-side (judgment call at execution time — do not force all four into handler-gating if the card is already readable).
- Ops prompt sections (`__init__.py` `_OPS_MODE_SECTIONS`): require the publish explanation to tie back to the triggering failure and be owner-readable; keep the stage flow (identify → fix_approve → fix_publish) intact.
- Manifest drift cleanup: `luna-plugin.toml` tool list/policies and `db_tables` (missing `playbook_fix_proposals`) brought in line with code.

Tests green (305 baseline + new). Bump three version stamps.

## Manual verification (with 012 phase 4)

QA Luna, open browser: seed a playbook that fails on live run → readable proposal card in ops → approve → agent (with tools) fixes candidate, runs specs → publish card: plain-English explanation + collapsed diff → approve → live + ops announcement. Screenshots + transcript in the execution summary.

## Ship

Push (gh auth switch to huemorgan2), then publish 0.30.0 to marketplaces.com.ai (publish-plugin skill; token from workspace .env).
