# Phase 0 — baseline

## Scope

No code changes. Record the pre-change state of the repo and test suite in
the 014 worktree so later regressions are attributable.

## Deliverables

- Worktree `.claude/worktrees/014-failed-run-awareness` on branch
  `014-failed-run-awareness`, based on main @ cd7a09f (== origin/main).
- Full pytest run result recorded in the execution summary.
- Version-stamp inventory (all three): pyproject.toml,
  `plugin_playbooks/__init__.py` PluginManifest, `plugin_playbooks/luna-plugin.toml`
  — all currently 0.20.0. Target version for this plan: 0.21.0.

## Verification criteria

- `uv run --extra dev pytest` completes; count and any pre-existing
  failures recorded.
