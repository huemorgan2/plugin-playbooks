# 030 — Execution summary

Shipped in plugin-playbooks **0.45.0** (commit ea1ace6, published to
marketplaces.com.ai). Core half: luna **0.92.026** commit 3b709d0
(plans/103, `Approvals.request_nowait`).

## What changed

- `_require_publish_approval` feature-detects `request_nowait` on the
  approval engine. New core: card raised without parking; `pending` →
  `awaiting_owner_approval` JSON with the WOKEN / do-not-retry contract;
  inline grant-hit and rejection behave exactly as before. Old core:
  byte-for-byte the previous parked `request()` path (900s timeouts kept).
- Wake destination: `ctx.current_conversation_id`, falling back to the ops
  conversation when headless.

## Verification

- `tests/test_approval_wake_on_decision.py` — 5 tests (pending contract,
  inline approve, rejection, current-over-ops targeting, old-core
  fallback). Full suite: 387 passed.
- QA E2E on a real Luna (core 0.92.026 + 0.45.0, port 8766):
  `playbook_publish` returned in **136 ms** with awaiting_owner_approval;
  agent ended its turn; owner approve → orphan wake → detached
  continuation turn re-issued the publish → auto-approved via the
  short-TTL `target=""` pre-grant → `{"status": "published",
  "live_version": 2}`. Exactly one card, one wake, one publish.

## Double-trigger audit result

Confirmed structural exactly-once: nowait rows register no waiter, so the
waiter resume path cannot fire; the orphan wake is the sole delivery. QA
run showed no duplicate card and no duplicate continuation.

## Deployment

- Scanny (vaselin-scanny-2) upgraded 0.44.0 → 0.45.0 (2026-09-04).
- Fleet pin: plugin-playbooks 0.45.0 sha256 1342179b… pinned via
  rollout_image.py; baked into image 0.92.026-r1.
