# Phase 2 — execution summary

## What shipped

`plugin_playbooks/fix_proposals.py`:

- The fix-proposal approval card gained a luna-094 `presentation`:
  eyebrow "Playbook failing", headline
  `"'{display}' is failing — approve a fix attempt"`, and a two-paragraph
  owner-language explanation — what the playbook does (first manifest line,
  else its description), which step failed on a real run, and that approving
  starts a fix attempt while nothing goes live without a second approval.
  Run id / version / step / error head moved behind the fold as one
  `changes[]` text block ("Failure detail"). `summary` shrank to a one-line
  fallback for surfaces without presentation support. Payload unchanged —
  dedup identity is untouched.
- Bug fix (silent card loss): `getattr(ctx, "approval", None)` propagated
  the RuntimeError that `ctx.approval` (a property on the real
  PluginContext) raises when the approval engine is unwired, and the outer
  catch-all ate the card AND the log line. Access is now under try/except
  with fallback to the documented ledger-only path.
- Bug fix (tool-free wake): the approved-fix wake
  (`send_muted_message(channel="moment")`) passed no `tools`, so the
  reaction turn could not diagnose anything. It now passes `tools="all"`;
  scoping is the core's job — conversation kind/state ride into moment
  turns since luna 0.91.000 (shipped earlier in this plan's parent work).

Tests (`tests/test_build_operate.py`, +3):

- Full card path via a fake wired ctx: presentation shape (eyebrow,
  headline, purpose + failed step + second-approval promise in the
  explanation, error text only behind the fold), one-line summary, payload
  identity, ledger row flipped to "approved", wake carries `tools="all"`,
  the ops conversation id, and `channel="moment"`.
- Denied card → proposal "dismissed", no wake.
- ctx whose `approval` property raises RuntimeError → no exception, ledger
  row survives as "open" (regression test for the silent-loss bug).

## Verification

Full plugin suite: **316 passed** (313 after phase 1 + 3 new), zero
failures. Live-browser verification deferred to the combined E2E (master
plan phase 4), per PHASE.md.

## Deviations from PHASE.md

None.

## Surprises / learnings

- The failing-step purpose line falls back cleanly for manifest-less
  playbooks (the test seed has only a description) — no empty parentheses
  in the card.

## Reassessment of remaining phases

No changes. Phase 3 as planned: `why` steering args on the remaining
mutating tools, `_OPS_MODE_SECTIONS` prompt sections, luna-plugin.toml
drift cleanup (tool list/policies, missing `playbook_fix_proposals` in
db_tables), and the 0.30.0 bump across all three stamps.
