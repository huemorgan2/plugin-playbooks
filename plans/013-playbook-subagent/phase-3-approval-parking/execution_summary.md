# Phase 3 — approval parking surfaced: execution summary

## What shipped

Still version **0.25.0** (batched with phase 2's stamp — nothing shipped
alone between them; the marketplace publish happens at plan close). All
changes on branch `013-playbook-subagent`.

- `plugin_playbooks/delegation.py`
  - `_GATED_TOOLS` — the plugin's prompt_always tools. Writing the drift
    test immediately caught rot in the hand-written first draft: it had
    `playbook_ack_failures` (not actually gated) and was missing
    `playbook_run_candidate` (gated). The shipped set matches the ToolDefs.
  - `waiting_on_owner(events, now) -> str | None` — PARKED is derived, never
    a status: the last event is a gated tool call with no result (`ms is
    None`) older than 8 s. Statuses stay running/done/failed/needs_owner.
  - `_delegation_payload` now prefers the **live in-process feed** over the
    DB row while running (see surprises), and when waiting adds
    `waiting_for_approval` (tool name, machine field) plus a PAUSED message
    that hands the model **owner words** ("make the change live"), with an
    instruction never to say the tool name (vocabulary rule).
  - `_GATED_TOOL_OWNER_WORDS` — tool → owner words, drift-tested to cover
    exactly `_GATED_TOOLS`.
- `plugin_playbooks/routes.py` — card payload gains `waiting_for_approval`
  (None unless running and waiting).
- `plugin_playbooks/card.py` — amber banner between the phase rows and the
  detail feed: "Waiting for your approval — <owner words>", with its own JS
  owner-words map (fallback: tool name with underscores stripped). Shows
  only while `status === 'running' && waiting_for_approval`; clears itself
  when polling sees the wait resolved.
- Tests: `tests/test_waiting.py` (9 — detection cases, gated-set drift test
  scanning agent_tools.py for `policy="prompt_always"`, owner-words drift
  test, live-feed-over-stale-row regression, no-tool-codes message test),
  plus a waiting test in `tests/test_card_route.py` and a banner-wiring
  test in `tests/test_card_html.py`. Full suite: **251 passed**.

## Verification on real Luna (QA, port 8766)

Three delegations in conversation "p3 approval parking dojo", each editing
qa84-hello's greeting and promoting (v3, v4, v5), each parking on the
`playbook_promote` approval, driven end to end in a real Chrome via CDP:

- **Parked, in the browser**: the card showed the amber banner "Waiting for
  your approval — make the change live" with the Ship dot amber, and the
  plugin-approvals card sat right below it in the chat
  (`p3-browser-01-parked.png`).
- **Status tool while parked**: a real "Status update?" turn made Luna call
  `playbook_agent_status`, whose payload carried
  `waiting_for_approval: "playbook_promote"` and the PAUSED message; Luna
  relayed it and ended its turn (`p3-browser-04-parked2-status.png`). After
  the owner-words change, run 3's relay read "Paused waiting for your
  approval to make the v5 change live" — no tool code reached the owner.
- **Approve → resume**: clicking "Just this once" on the approval card in
  the real browser resumed the parked delegate; the delegation finished
  `done`, the banner cleared, the card flipped green with all four phases
  lit and the result panel showing the report
  (`p3-browser-02/03-*.png`). API approval verified too (run 2).
- Zero page console errors across all runs (`window.__errs` empty).

## Deviations from PHASE.md

- The PAUSED status-tool message now speaks owner words instead of the tool
  code (PHASE.md had the raw name in the message) — the browser run showed
  Luna echoing `playbook_promote` verbatim to the owner, which the
  vocabulary rule exists to prevent.

## Surprises / learnings

- **The throttled DB flush hides the park.** A delegate that parks right
  after a gated call never flushes that last event (nothing further happens
  to trigger the flush), so the DB row's feed is missing the very event
  that signals the wait. First live status check reported plain "running".
  Fix: `_delegation_payload` reads `_LIVE_FEEDS` first, exactly like the
  card route. Found only because the check ran on a real parked delegate.
- **Drift tests pay for themselves at write time**: the gated set was
  already wrong (one extra, one missing) before the test existed.
- QA browser sessions die with the server restart's cookie: re-auth by
  writing the minted JWT into `localStorage["luna.token"]` via CDP (the
  SPA's own storage key) — the httpOnly `luna_token` cookie alone doesn't
  log the SPA in.

## Reassessment of remaining phases

- **Phase 4 (dojo end-to-end)**: unchanged in scope — a fix-task on a
  broken playbook so Understand→Change→Prove→Ship light up in one run,
  compared against the plans/012 baseline. The three phase-3 runs already
  exercised the full happy path incidentally (all four phases lit), so
  phase 4 focuses on the *fix* framing (broken spec → diagnose → repair)
  and the plan-close checklist (merge, push, publish 0.25.0, master
  summary).
