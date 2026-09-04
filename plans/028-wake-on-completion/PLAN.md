# 028 — Wake-on-completion for playbook runs

## Problem

`playbook_run` is hybrid-async (plans/009): it waits up to 55s, then tells the
agent to poll `playbook_status`. Polling burns a tool call per check, and if
the turn ends (SSE death, containment, owner stop) before the run finishes,
the outcome is orphaned — nobody ever reports it. Claude Code and modern
harnesses solve this with wake-on-completion: the background job finishes and
the harness starts a new model turn carrying the result.

## Investigation result: core already has the mechanism

No luna-core change is needed. The pieces exist:

- `ctx.send_muted_message(title, content, channel="moment", respond=True,
  tools=..., conversation_id=...)` (context.py:233 → muted.py:252
  `post_muted_message`) persists a muted row **and runs a real agent turn**,
  with queue-if-busy (turn registry → inbox → drain legs), containment,
  outcome stamping, open-loops digest, ops fallback, and detached delivery.
  The scheduler's `agent_prompt` fires use exactly this.
- `channel="awareness"` persists the row with **no turn** — a zero-token
  notice.
- The runner already emits `playbook.run.completed` on every driven terminal
  transition (runner.py `_complete_run`), and `FixProposalService`
  (fix_proposals.py) already subscribes to it and **wakes the agent on
  failures** via a moment. Wake-on-completion is the same pattern, widened.
- Queue-if-busy gives a free bonus: if the originating turn is *still alive
  and polling* when the run completes, the wake queues into that
  conversation's inbox and the active turn drains it — the result is injected
  into the waiting turn instead of starting a second one.

What is missing, all plugin-side:

1. Nothing subscribes to `playbook.run.completed` for successes, and the
   payload lacks `playbook_name`, `trigger`, `conversation_id`.
2. `playbook_run`'s timeout branch promises nothing — it demands polling.
3. No record of "the agent is owed a wake" that survives a restart
   (`sweep_orphaned_runs` marks orphans failed *quietly by design*).

## Contract

**Agent-launched runs** (`trigger="agent"`, via `playbook_run`):
- Fast runs (finish inside the wait window): unchanged — result returns
  inline in the tool result. No wake, no double-report.
- Slow runs (wait window expires) and `wait_seconds=0` fire-and-forget: the
  run row is stamped `wake_on_complete=true`, and the tool reply becomes:
  "Still executing in the background. You will be woken with the result when
  it finishes — do NOT poll, do NOT re-run, and do NOT report results yet.
  You may end your turn." (`playbook_status` remains available for an
  explicit user ask.)
- On terminal status, the wake service sends a **moment** to
  `run.conversation_id` (fallback ops): playbook name, run_id, final status,
  duration, error on failure, and compact step outputs on success (same
  shape as `playbook_run`'s inline done-branch, size-capped).

**Background runs** (webhook triggers, scheduler `action_type="playbook"`,
subtasks): no moment on success — Scanny takes dozens of trigger runs a day
and a turn per run is token burn. Instead an **awareness** note to the ops
chat (zero tokens, collapsed row) per plan-101's "ops chat is THE events
inbox" contract. Failures already get a moment via `FixProposalService` —
no double-moment from this service.

**Excluded**: `is_test` runs (candidate grading is interactive) and
`parent_run_id` subtask runs (the parent run reports).

**Restart safety**: `wake_on_complete` lives in the DB. `sweep_orphaned_runs`
honors it — a swept run with the flag gets a failure moment ("run was
orphaned by a restart") instead of dying silently. The promise survives the
process.

## Changes

1. `models.py` — `PlaybookRun.wake_on_complete: bool` (default false);
   register in the existing `_ensure_columns` ALTER TABLE helper
   (`__init__.py:64`) as `BOOLEAN DEFAULT FALSE`.
2. `runner.py` `_complete_run` — enrich the `playbook.run.completed` payload
   with `playbook_name`, `trigger`, `conversation_id`, `wake_on_complete`
   (row is already re-read there). Keep the `_active_run_id` neutralization.
3. `runner.py` `sweep_orphaned_runs` — after marking a flagged orphan failed,
   emit the same completion event (flagged runs only; unflagged orphans stay
   quiet as today).
4. New `wake.py` — `RunCompletionWake`, mirroring `FixProposalService`
   line-for-line: `events.subscribe("playbook.run.completed", ...,
   background=True)`, filter per contract, re-read the run row for outputs,
   `send_muted_message(...)` guarded with `getattr` for old cores. Moment
   containment mirrors plugin-tasks resume defaults (bounded max_turns /
   token_budget / timeout_s).
5. `agent_tools.py` `_run` / `_run_candidate` — stamp `wake_on_complete=true`
   when returning with status "running"; rewrite the timeout-branch text and
   the ToolDef description (drop "poll playbook_status", promise the wake).
   Candidate runs are `is_test` → stamped false, text unchanged.
6. `__init__.py` — construct/start the wake service next to
   `FixProposalService`; version → **0.44.0 in all three stamps** (pyproject
   is at 0.43.0 but luna-plugin.toml and the manifest sit at 0.42.0 — fix
   the drift in the same commit).

Out of scope: the stale in-tree copy `luna/plugins/plugin_playbooks/`
(managed_plugins overrides it on every real tenant); `wait_for_event` stub;
making background-run moments configurable per playbook.

## Tests

- Slow agent run: `_run` times out → row flagged → completion → exactly one
  moment to the origin conversation carrying status + outputs.
- Fast agent run: finishes in-window → flag false → no wake.
- Webhook-trigger run success → awareness to ops, no moment; failure → no
  moment from wake service (fix-proposals owns it), awareness still lands.
- `is_test` / subtask runs → nothing.
- Sweep: flagged orphan → failure moment; unflagged → quiet (existing
  `test_sweep_marks_orphaned_runs_failed_but_spares_live_ones` stays green).
- Old core (ctx without `send_muted_message`) → no crash, tool falls back to
  the poll text.
- Existing suites green (test_async_run, test_failed_run_awareness, etc.).

## Ship

Commit → push (huemorgan2) → publish 0.44.0 to official → upgrade Scanny →
live verify: launch a deliberately slow playbook from an agent turn via the
dojo/API, let the wait window lapse, confirm the wake moment lands and the
agent reports the result; confirm a webhook run leaves only an awareness row
→ fleet pin → execution summary.
