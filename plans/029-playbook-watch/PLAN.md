# 029 — `playbook_watch`: wake me when a playbook's next run finishes

luna-plugins plans/017 Item 5. An agent that is waiting for an EXISTING
playbook to be run by something else (webhook, scheduler, the owner, a
different chat) today has no way to hear about it — it polls
`playbook_status` or goes dark. This adds a one-shot durable watch that
rides the proven 028 wake machinery.

## Design

- New model `PlaybookWatch` (`playbook_watches`): `id`, `playbook_id`
  (FK, CASCADE), `conversation_id` (stamped from the calling turn at
  block time — never reconstructed), `note` (nullable, echoed back in
  the wake so the agent remembers why it watched), `created_at`,
  `expires_at` (default now+7d), `consumed_at` (nullable).
- Tool `playbook_watch(name, note=None)`: resolves the playbook by
  name (live only), inserts the row, returns "You'll be woken when
  '{name}' next finishes a run — end your turn. The watch expires in
  7 days; cancel with playbook_watch_cancel." One active watch per
  (playbook, conversation): re-watching refreshes `expires_at`/`note`
  instead of inserting a duplicate.
- Tool `playbook_watch_cancel(name)`: consumes the caller
  conversation's active watch; listing rides `playbook_status` (a
  `watches` section when any active watch exists for the caller).
- Delivery: `RunCompletionWake._deliver_inner` gains a watch pass that
  runs for EVERY completion payload (any trigger, before the
  wake_on_complete/awareness branches decide anything else): select
  active watches for `playbook_id` (un-consumed, un-expired), claim
  each via `UPDATE … SET consumed_at WHERE consumed_at IS NULL`
  (exactly-once under concurrent completions), moment per watcher
  conversation with the 028 body + the watch note, 028 caps.
- Expired watches: reaped opportunistically in the watch pass (delete
  where expired), nothing new scheduled.
- Old-core tolerance: the watch pass lives behind the existing
  `send_muted_message` feature-detect (no send → rows stay for later).
- Tool description language: ONLY "wake me when an existing playbook's
  next run finishes." Nothing may steer the agent toward creating
  playbooks as event-listening plumbing (017 standing rule 6); raw
  external-event watching stays out of scope.

## Double-trigger audit (017 standing rule 1)

| Existing path | Fate |
|---|---|
| 028 `wake_on_complete` launcher moment | Can coincide with a watch on the same run. Dedupe per conversation: launcher conversation == watcher conversation → ONE moment (the launcher moment wins, watch consumed silently). Different conversations → each gets its own (they asked separately). |
| Un-flagged agent run (tool returned result inline) | Watcher in the SAME conversation already has the result — consume silently. Different conversation still gets its moment. |
| FixProposalService failure moment (ops chat) | Keeps exclusive ownership of background-run failures in ops: watcher conversation == ops conversation and run failed → fix-proposal moment wins, watch consumed silently. Watcher elsewhere gets its failure moment. |
| Ops awareness row for background runs | Keep — passive log, not a turn. |
| Test runs / subtask runs | `_on_completed` already drops them before delivery; watches never fire for them (watch means "a real run"). |

Net rule: per (run, conversation) at most ONE moment, and ops failure
ownership is never duplicated.

## Tests

- Watch → background completion → exactly one moment to the watcher,
  watch consumed.
- Launcher==watcher conversation → one moment only.
- Watcher==ops + failed background run → fix-proposal moment only
  (watch consumed silently, no second moment).
- Expired watch → no moment, row reaped.
- Two concurrent completions → single consume (claim race).
- Re-watch refreshes instead of duplicating.
- Agent-trigger inline result, same conversation → silent consume.
