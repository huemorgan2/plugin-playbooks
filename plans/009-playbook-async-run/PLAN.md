# 009 — plugin-playbooks 0.5.0: async runs + tools guide

## Problem (verified, not taken on faith)

An agent reported `playbook_run` unusable for long playbooks. Confirmed in code:

1. **`playbook_run` blocks to completion.** `PlaybookRunner.start_run`
   (runner.py:89) awaits `_execute_steps` before returning, and the tool has
   `timeout_seconds=120`. Any playbook slower than 120s → `TOOL FAILED:
   Timeout`, **no run_id returned**, run keeps executing orphaned.
2. **The HTTP run route and event triggers block too.** `routes.py:501` hangs
   the UI "Run" request for the whole run; `triggers.py:75` blocks the event
   bus handler for the duration of a run.
3. **`playbook_cancel` is cosmetic.** `_PlaybookCancel` is defined but *never
   raised*; `cancel_run` only flips the DB status — the coroutine keeps
   executing every remaining step.

The reporting agent asked for pure fire-and-forget. We do **hybrid** instead:
short playbooks (the common case) keep their one-call UX; long ones return
early with a run_id. Pure fire-and-forget would force a poll loop on every
run, even a 2-second one.

## Design

### Runner: split creation from driving

- `_create_run(...)` → DB record + `playbook.run.started` / `activity.started`
  events, returns `PlaybookRun` (status `running`).
- `_drive_run(run, playbook, inputs)` → current execution body (heartbeat,
  `_execute_steps`, `_complete_run`, `activity.completed`), plus
  `except asyncio.CancelledError` → mark run `cancelled` (real cancel support).
- `start_run(...)` — **unchanged signature & blocking semantics** = create +
  await drive. Subtask composition (runner.py:601) and existing callers keep
  working untouched.
- `start_run_background(...)` = create + `asyncio.create_task(_drive_run)`;
  task registered in `self._tasks[run.id]`, popped via done-callback. Returns
  the run immediately.
- `wait_for_run(run_id, timeout)` — `asyncio.wait` on the task (no cancel on
  timeout), then re-read the run from the DB. Returns the run either way.
- `cancel_run(run_id)` — cancel the registered task (drives the
  CancelledError path); keep the DB flag write as fallback.

### Tool: `playbook_run` becomes hybrid-async

- Calls `start_run_background`, then `wait_for_run(run.id, wait_seconds)`.
- Default `wait_seconds=55` (well under the 120s tool timeout); new optional
  `wait_seconds` param (0–90; 0 = pure fire-and-forget).
- Finished within the window → **exact same result shape as today**
  (`status`, `step_results` / failure guidance) — no breaking change.
- Still running → `{run_id, status: "running", message: poll
  playbook_status(run_id)}`. Tool description documents the pattern.
- `playbook_status` gains run-level `error`, `trigger`, `started_at`,
  `completed_at`, and a `still_running` hint so polling agents see progress
  (per-step statuses were already there).

### Route + triggers go background

- `POST /playbooks/{name}/runs` → `start_run_background`; responds
  `{run_id, status}` immediately (UI already tracks liveness via
  `activity.*` heartbeats — no UI change needed).
- Trigger handlers → `start_run_background` (bus handler no longer blocked;
  failures already logged inside the drive body).

### UI: ⓘ tools guide ("[I] section")

In `PlaybooksSection` header, an `Info` icon button toggles a collapsible
panel: a short "what playbooks can do" paragraph, then a **table of all 10
agent tools** (tool → what it does) so owners can understand the machinery
Luna uses on their behalf.

## Version & ship

- 0.4.0 → **0.5.0** in `luna-plugin.toml`, manifest (`__init__.py`),
  `pyproject.toml`.
- Tests: new `test_async_run.py` — background run completes; wait window
  returns early result; slow run returns `running` then finishes; cancel
  actually stops execution mid-run; subtask still blocks.
- Rebuild UI → package zip → publish to marketplaces.com.ai slug `official`
  → commit/push plugin repo → bump submodule pointer in luna-plugins → push.

## Out of scope (noted for later)

- Startup sweep marking runs stuck in `running` after a server crash.
- Per-step live streaming into chat while polling.
