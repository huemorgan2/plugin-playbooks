# Phase 1 — promote hotfix (0.27.1)

## Baseline (recorded before any change, 2026-08-30)

- Checkout: plugin-playbooks `fa9615e` (0.27.0), clean tree, fast-forwarded
  from 0.7.0 earlier today.
- `uv run --with httpx python -m pytest tests/ -q` → **271 passed**.
  (`httpx` is in the `dev` extra but was not in the venv; `--with` is the
  workaround, not a code change.)
- `npm test` in `ui-src/` → **111 passed / 13 files** after `npm install`
  (the tree lacked `jsdom`; before the install 3 test files failed to start
  their worker — an environment gap, not a regression).
- Local QA Luna: not running at the start of the phase.

## Scope

Fix "Promote on an old version does nothing" — plan Part A.

1. `publish.py`: for restores/rollbacks (`include_live=True`) drop the
   `since` bound — any completed run of the exact version is evidence,
   because version rows are immutable and snapshot rows are minted at the
   *next* edit (so their `created_at` post-dates every run they had).
   Candidate publishes keep the strict "run after the row was created" rule.
   One place: `test_run_gate` decides the bound, so the promote route, the
   rollback route and the `playbook_publish` tool all inherit it.
2. UI `VersionsPanel.handlePromote`: stop swallowing the error — render
   `promoteRefusalMessage(e)` under the panel header, clear on next attempt.
3. Tests (pytest): restore passes with a green run older than the row's
   `created_at`; restore with no run at all → refused (`gate: test_run`);
   restore whose latest run failed → refused; candidate still refused when
   its only green run predates the row. Vitest: `promoteRefusalMessage`
   already covered — the panel's banner gets a render test.

## Out of scope

Everything in Parts B/C. No new routes, no schema.

## Verification

- Full pytest + vitest suites green (no regressions vs baseline).
- Real environment: load 0.27.1 on the local QA Luna, create a playbook
  with two versions, run the old one once, restore it via
  `POST /playbooks/{name}/promote {version: N}` → 200 and `live_version`
  flips; then a version with no run → 422 with `gate: test_run`, and the
  panel shows the refusal in the browser (CDP). If the local Luna cannot be
  brought up in this phase, say so in the summary — do not claim it.

## Ship

Bump 0.27.1 in `luna-plugin.toml`, `pyproject.toml`, `__init__.py`
manifest; rebuild `plugin_playbooks/ui/`; commit; push; publish to the
marketplace (`publish-plugin`, slug `official`); repin luna
`plugin-set.toml` to 0.27.1 with the published sha256.
