# 030 — Publish approval: wake-on-decision instead of parking

Part of luna-plugins plans/017 (wake everywhere), item 1. Core half: luna
plans/103 (`Approvals.request_nowait`, shipped in luna 0.92.026).

## Problem

`_require_publish_approval` parks inside the tool handler on
`ctx.approval.request()`. The park sits under the ToolDef timeout — the
900s band-aid (0.30.3) merely stretched the fuse. A handler that dies
before the owner answers orphans the card, and the agent burns its turn
waiting either way.

## Design

Feature-detect `request_nowait` on the approval engine:

- **New core (>= 0.92.026):** raise the card via `request_nowait`. Inline
  short-circuits (grant hit / recent rejection) behave exactly as before.
  On `pending`, return a non-failure JSON: "awaiting the owner's approval
  — you will be WOKEN when they decide; do NOT retry, do NOT poll; finish
  anything else and end your turn." The core's orphan-resume wake spawns
  the continuation turn on decision; the re-issued publish auto-approves
  against the short-TTL `target=""` pre-grant (plans/103 widening).
- **Old core:** fall back to the parked `request()` contract unchanged —
  hence `timeout_seconds=900` STAYS on `playbook_publish` /
  `playbook_rollback` and `tests/test_tool_timeouts.py` stays valid.

Wake destination: `ctx.current_conversation_id` (the turn's own chat)
falling back to the ops conversation when headless — the card and the
continuation land where the agent was actually working.

## Double-trigger audit

- One decision → one delivery: nowait rows have no in-process waiter, so
  the waiter path cannot fire; the orphan wake is the only resume, shared
  with the died-park path (exactly-once is structural, luna plans/103).
- The pending JSON explicitly forbids retry/poll, so the agent does not
  mint a duplicate card (dedup would join it to the same id anyway).
- Old-core path is byte-for-byte the previous behavior.

## Tests

- New core: pending decision → tool returns awaiting_owner_approval JSON,
  nothing published, no park.
- Grant hit via request_nowait → publish proceeds (returns None).
- Rejection via request_nowait → existing rejection JSON.
- Old core (engine without request_nowait) → request() park path used.
- test_tool_timeouts.py unchanged (>= 300s still asserted).
