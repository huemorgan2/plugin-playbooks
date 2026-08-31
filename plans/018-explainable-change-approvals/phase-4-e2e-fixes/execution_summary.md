# Phase 4 (unplanned) — E2E-driven fixes: 0.30.1, 0.30.2, 0.30.3

Not in PLAN.md. Master plan 012 phase 4 (luna-plugins repo,
plans/012-readable-ops-approvals) ran the whole fix-proposal → publish loop
on a real QA Luna (0.91.001, port 8767, real Claude agent, real browser via
CDP). Three bugs surfaced that the 321-test unit suite could not see; each
shipped as its own version the same day.

## 0.30.1 — approved cards were dismissed (commit 2286e9e)

`_post_card` read `getattr(result, "approved", None)`, but the real
`ApprovalDecision` (luna/approval/contract.py) carries `decision: str`
("approved"/"rejected") and has NO `approved` attribute — every owner
approval was recorded as "dismissed" and no wake fired. Unit tests passed
because the `_CardDecision` stub had the same wrong shape. Fixed to accept
both shapes; the stub in tests/test_build_operate.py now mirrors the real
contract and documents why.

## 0.30.2 — wake turn was stuck in diagnose-only mode (commit 6c5adde)

The wake turn inherits the ops chat's STORED state. In `identify`, mode
gating strips every mutation tool and `tools="all"` cannot override it — the
agent woke able to describe the fix but not apply it. Fix: on approval,
advance the ops chat `identify → fix_publish` via the new luna 0.91.001
`ctx.set_conversation_state(..., only_from="identify")` before the wake
(degrade-visible on older cores). `fix_publish` was chosen over
`fix_approve` because only it carries `playbook_publish` — the rich publish
card IS the promised second approval.

## 0.30.3 — publish died on the tool timeout (this commit)

`playbook_publish` raises the owner's approval card INSIDE its handler and
parks on the decision, but the ToolDef used the default
`timeout_seconds=30`: the runtime's `asyncio.wait_for` cancelled the parked
handler after 30s, the card stayed pending (orphaned — a late approval
resumed nothing), and the publish never executed. The agent retried, filed a
second orphan card, and correctly reported itself stuck. Fix:
`timeout_seconds=900` on `playbook_publish` and `playbook_rollback` (same
in-handler approval path). Pinned by tests/test_tool_timeouts.py.

## Verification

- 321 unit tests green (includes the new timeout pin).
- Full live loop on QA Luna 8767: scheduled `site-status-brief` failure →
  readable fix-proposal card → owner approve → state flip to fix_publish →
  agent edited (Jinja `default` filter), dry-ran, green candidate run →
  ONE `playbook_change` card (plain-English explanation + testing evidence
  up front, code diff collapsed) → owner approve in the browser → publish
  gates green → live_version 1 → 3 → announce in the ops chat.

## Learnings

- Any tool that parks on a human decision must own its timeout; the 30s
  default is for machine work.
- A cancelled in-handler approval leaves a pending card no one can consume.
  If this bites again, consider request-scoped supersede/reclaim in the
  approvals engine (luna-side) rather than longer timeouts.
