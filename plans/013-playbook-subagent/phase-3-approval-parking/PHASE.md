# Phase 3 — approval parking surfaced

## What phase 2 already proved

The round-trip itself works with zero new code: the delegate called
`playbook_promote` (prompt_always, medium), the approvals plugin posted an
approval card in the owner's chat, the contained `run_turn` parked, an API
approve resumed it, and the delegation finished `done`. What is missing is
*visibility*: while parked, the progress card and `playbook_agent_status`
just say "running", so the owner has no cue that the delegate is waiting on
them — the exact silent-turn trap in the approval-gates-park-turns memory.

## Scope

Derive a "waiting on you" state from the event feed — never from a new
status value (statuses stay running/done/failed/needs_owner; the model
never emits a PARKED code — vocabulary rule).

1. `delegation.py`
   - `_GATED_TOOLS`: the plugin's own prompt_always tools
     (set_autonomy, edit_force, manifest_set, promote, rollback,
     spec_delete + any others found by the drift test). A drift test scans
     `agent_tools.py` for `policy="prompt_always"` ToolDefs so the set
     cannot rot silently.
   - `waiting_on_owner(events, now) -> str | None`: last event is a tool
     line with no result (`ms is None`) on a gated tool, older than 8 s →
     that tool name; otherwise None.
   - Card payload (route) and `playbook_agent_status` gain a
     `waiting_for_approval` field (tool name in the status tool; owner
     words on the card). The status-tool message tells the agent to point
     the owner at the pending approval card.
2. `card.py` — while running with `waiting_for_approval`: the active phase
   dot goes amber-pulse, and a one-line banner appears: "Waiting for your
   approval — <owner words>" using a small tool→owner-words map
   (promote → "make the change live", rollback → "roll back the live
   version", etc.; fallback: the tool name). Banner clears when polling
   shows the wait resolved.
3. Orphan sweep unchanged — a parked delegate that dies with the server is
   already marked failed on next load.

## Verification

- Unit: waiting detection (pending gated call old enough / too fresh /
  resolved / non-gated), payload fields, card HTML contains the banner
  wiring, gated-set drift test. Full suite green.
- Real Luna (8766, CDP browser): drive a delegation whose task requires a
  promote (edit qa84-hello greeting + promote). While parked: card shows
  the waiting banner in the open browser and `playbook_agent_status`
  reports `waiting_for_approval`. Approve from the chat approval card
  (browser click if reachable, else API + note). Card returns to plain
  running, then lands done. Screenshots of parked and resumed states.
