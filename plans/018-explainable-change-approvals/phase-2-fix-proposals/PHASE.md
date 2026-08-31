# Phase 2 — fix-proposal loop: readable + actually working

## Scope

`plugin_playbooks/fix_proposals.py` only.

1. **Readable card** — the fix-proposal approval gains a luna-094
   `presentation`: eyebrow "Playbook failing", headline
   `"'{name}' is failing — approve a fix attempt"`, explanation in owner
   language (what the playbook does — first line of its manifest or its
   description; which step failed; that approving starts a fix attempt and
   publishing still requires a test run + a second approval). Raw run id /
   version / step / error head move into `changes[]` (`kind:"text"`) behind
   the fold. `summary` shrinks to a one-line fallback for old UIs.
   Dedup note: luna keeps the FIRST pending row's presentation, so the card
   always reflects the first failure; repeats bump the ledger count only.
2. **Bug: silent card loss** — `getattr(ctx, "approval", None)` propagates
   the RuntimeError `ctx.approval` raises when the engine is unwired, and
   the outer catch-all eats the card. Access under try/except and fall back
   to the documented ledger-only path.
3. **Bug: tool-free wake** — the approved-fix wake
   (`send_muted_message(channel="moment")`) passes no `tools`, so the
   reaction turn cannot diagnose anything. Pass `tools="all"`; scoping is
   the core's job (089 state gating; kind/state now ride into moment turns
   since luna 0.91).

## Verification

- New tests in `tests/test_build_operate.py` (the existing fix-proposal
  suite lives there): full card path via a fake ctx — presentation shape on
  the request; RuntimeError-raising `approval` property degrades to the
  ledger row (no exception, proposal still filed); approved wake carries
  `tools="all"` and the ops conversation id.
- Full plugin suite green (313 baseline after phase 1).

## Ship

Batched into 0.30.0 with phase 3.
