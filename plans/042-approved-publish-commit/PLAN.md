# Commit an approved candidate publish

## Problem

The `approval.decided` subscriber records the owner's decision but does not publish. The generic Luna wake can omit the reissued tool call and still tell the owner the candidate is live. The LONG crash/restart trial reproduced this: approval was approved, candidate 1 persisted, live version stayed 0.

## Change

- Subscribe to `approval.orphan_decided`, emitted only after the approval engine creates the exact-payload short-lived grant.
- For a publish action, match approval id, playbook name, version, and remembered card. Reject stale, edited, or mismatched cards. For an approved card, call the registered `playbook_publish` handler, which rechecks static validation, test-run evidence, probes, candidate identity, approval grant, and stored read-back.
- Deliver the resulting verified live version or exact refusal to the originating conversation. An approval is never described as publication until the read-back succeeds.
- Keep the handler idempotent: a duplicate decision after the candidate is consumed must not publish another version.

## Verification

- Focused tests cover matching approved card, rejected/stale/wrong-version decisions, duplicate delivery, and the real publish gate with a grant.
- Run the full Playbooks suite, the full Luna/default-plugin suite, and dojoP LONG recovery on a fresh database.

Focused wake/publish tests: 24 passed. Full Playbooks suite: 775 passed, 8 warnings (`/private/tmp/playbooks-0579-full-final-20260917.log`). Full Luna suite with all eighteen image-set plugins: 2,832 passed, 41 skipped, 11 warnings. Live recovery is in progress.
