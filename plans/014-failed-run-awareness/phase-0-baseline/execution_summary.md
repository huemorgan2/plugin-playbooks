# Phase 0 — baseline: execution summary

## What was done

- Created git worktree `.claude/worktrees/014-failed-run-awareness`,
  branch `014-failed-run-awareness`, based on main @ cd7a09f
  (`0.20.0: plans/012 phase 2`), which equals origin/main.
- Copied the (untracked) plan folder `plans/014-failed-run-awareness`
  into the worktree; it will be committed with phase 1.

## Baseline test state

`PYTHONPATH=. uv run --extra dev pytest -q` → **189 passed, 5 warnings,
0 failed** (10.5s). Note: plain `uv run --extra dev pytest` fails with
`ModuleNotFoundError: plugin_playbooks` — the project itself is not
installed into uv's ephemeral env; `PYTHONPATH=.` is how the suite runs.

## Version stamps (pre-change)

All three at **0.20.0**: `pyproject.toml`,
`plugin_playbooks/__init__.py` (PluginManifest), and
`plugin_playbooks/luna-plugin.toml`. This plan ships as **0.21.0**.

## Deviations

None.

## Reassessment of remaining phases

No changes. Useful discovery for phase 1: late-added columns go through
`_COLUMN_MIGRATIONS` in `plugin_playbooks/__init__.py` (explicit additive
ALTERs — `create(checkfirst=True)` skips existing tables), not just the
model class. The new `failures_acked_version` column must be added in
both places.
