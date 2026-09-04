# 028 — Wake-on-completion: execution summary

Shipped as **0.44.0** (2026-09-04). Agents no longer poll `playbook_status`
for slow runs: `playbook_run` promises a wake when the wait window lapses,
and `RunCompletionWake` delivers the outcome as a muted moment to the
originating conversation when the run finishes. Background trigger runs
leave zero-token awareness rows in the ops chat.

## Investigation (what made this cheap)

Luna core needed **no change** — `ctx.send_muted_message(channel="moment")`
already persists a note and runs a real agent turn with queue-if-busy
(turn registry → inbox → drain legs), containment, outcome stamping, and
the open-loops digest; the scheduler's `agent_prompt` fires and playbooks'
own `FixProposalService` (failure wakes) already use it. The whole feature
is plugin-side wiring around the existing `playbook.run.completed` event.

Queue-if-busy gives a free bonus: if the originating turn is still alive
when the run completes, the wake queues into that conversation's inbox and
the active turn drains it — result injection instead of a second turn.

## What shipped

- `PlaybookRun.wake_on_complete` column (durable; `_COLUMN_MIGRATIONS`
  ALTER on old installs — confirmed applied on Scanny at load).
- `playbook_run` stamps the flag when a run outlives `wait_seconds` (or
  fire-and-forget) on a wake-capable core, and replies "you will be WOKEN —
  do NOT poll, end your turn". Old cores (no `send_muted_message`) keep the
  poll contract. A stamp-window race re-reads status so a run that finishes
  during stamping still returns its result inline.
- `playbook.run.completed` payload enriched: `playbook_name`, `trigger`,
  `conversation_id`, `parent_run_id`, `wake_on_complete`.
- New `wake.py` `RunCompletionWake` (mirrors FixProposalService): flagged
  runs → moment to origin conversation (ops fallback) with status,
  duration, error or step outputs (4KB cap); unflagged background trigger
  runs → awareness row in ops; test runs, subtasks, and unflagged agent
  runs → silent. Failures of background runs stay FixProposalService's
  moment — no double-wake.
- `sweep_orphaned_runs` honors the promise across restarts: flagged
  orphans emit the completion event (→ failure moment); unflagged orphans
  stay quiet as before.
- Version-stamp drift healed: pyproject was 0.43.0 while luna-plugin.toml
  and the manifest were 0.42.0 (parallel-session commits bumped only some
  stamps). All three now 0.44.0.

## Verification

- 382 tests pass, including 10 new (`tests/test_wake_on_completion.py`):
  stamp+promise on slow runs, inline result on fast runs, poll fallback on
  old cores, moment routing/content for done+failed flagged runs, silence
  for test/subtask/unflagged-agent runs, awareness for background runs,
  sweep emitting for flagged orphans only.
- Live on Scanny: upgraded 0.42.0 → 0.44.0 (sha256 9e36d978…), hot-loaded,
  29 tools, no error; logs show `added wake_on_complete column` and
  `run_wake.started`.
- Fleet pin updated (job-dadb869t0dsc73ctint0) — bakes into the next image
  build.
- Moment path's delivery call (`send_muted_message` moment to a
  conversation) is the exact call FixProposalService has exercised in
  production; awareness path to be confirmed on the next natural trigger
  run (monitor watching).

## Learnings

- In-memory `sqlite+aiosqlite://` without `poolclass=StaticPool` hands
  concurrent sessions separate empty databases — the background-run +
  stamp-write concurrency in the new tests flaked with "no such table"
  until the pool was pinned.
- Parallel sessions had left 0.41–0.43 committed locally but unpushed and
  unpublished, with the three version stamps disagreeing. The push carried
  them up; the "three stamps" check belongs at the start of every release.
