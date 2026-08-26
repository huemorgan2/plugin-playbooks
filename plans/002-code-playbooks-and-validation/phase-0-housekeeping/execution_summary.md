# Phase 0 — execution summary (2026-08-26)

## What shipped

- Baseline test suite at 0.7.0: **29 passed** (invocation:
  `uv sync --extra dev && uv run python -m pytest tests/ -q` — plain
  `uv run pytest` fails because the project has no build-system table, so the
  package is only importable from cwd via `python -m`).
- `luna-plugin.toml` stamp aligned 0.6.0 → 0.7.0 (three-stamps miss from the
  0.7.0 commit); `aiosqlite` + `greenlet` added to dev extras (both were
  missing and required by the async-SQLite tests).
- Commit `99417b3`, pushed to huemorgan2/plugin-playbooks main.
- Published **0.7.0** to the official marketplace (was 0.5.1); catalog
  confirms `latest_version: 0.7.0`.
- Synced 0.7.0 into `~/.luna/managed_plugins/plugin_playbooks` (was 0.4.0 —
  QA Luna had been running very stale playbooks code), restarted QA Luna on
  :8766, re-minted the owner JWT (old one expired; saved to scratchpad
  `qa-token.env` as `TOKEN=`).

## Verified

`/api/plugins` on QA Luna: plugin-playbooks 0.7.0, enabled, no error,
11 tools.

## Learnings

- The live tool surface has an 11th tool the plan didn't list:
  `playbook_list_available_triggers`. No impact on phases, but tool-count
  assertions must use 11+.
- QA managed_plugins can silently lag far behind (0.4.0 vs 0.7.0) —
  every phase must re-sync before verifying.

## Reassessment of remaining phases

No structural changes. Phase 1 proceeds as planned. Note for phase 6 (UI):
ui-src has a vitest suite (`__tests__/events.test.ts`) — include it in the
phase's verification alongside the browser check.
