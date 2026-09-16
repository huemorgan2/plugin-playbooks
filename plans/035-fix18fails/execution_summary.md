# 035-fix18fails — execution summary (plugin-playbooks)

Master: dojoP `plans/0003_fix18fails/execution_summary.md`.

## Phase 1 — 0.57.2, commit 2fe80da on `fix18fails` (2026-09-16)

- `plugin_playbooks/wake.py`: `_WAKE_TOKEN_BUDGET` 200_000 → 1_500_000, `_WAKE_MAX_TURNS` 12 → 20 (the meter is
  cumulative across the turn's requests; core's `timeout_s` is the limiter). Failure moments (`_wake_moment`,
  `_watch_moment`) now append `_failure_detail(run_id)`: `Error type`, `Failing step: <id> (<kind>)`,
  `Step error`, `Step inputs` (600 chars) from the failed `PlaybookStepRun`, and `Traceback:` (tail, 1500 chars)
  from `PlaybookRun.traceback`; the closing instruction ends with "Then continue what the original request
  asked for — fix and rerun if the request said so; report honestly what you did and what you did not."
  Both sends inspect the result: `run_wake.moment_aborted kind=… reason=…` / `run_wake.moment_failed` instead
  of an unconditional "delivered" log.
- Stamps: pyproject, luna-plugin.toml, `PluginManifest.version` → 0.57.2. `uv.lock` (stale at 0.29.0) left
  untouched.
- Tests: `tests/test_wake_failure_detail.py` (6). Suite: 683 passed, 4 failed, 80 skipped — the 4
  (`test_delegation::test_main_turn_payloads_stay_small`, 3 × `test_repro_fixplaybooks_runtime` timing tests)
  fail identically on the unchanged branch.
- Merge target at the end: `v2-runtime` (the live line), not `main` — flagged to Roy.

Final (2026-09-16 17:35 IDT): whole suite × 3 on four models with this plugin at 0.57.2 in the bench set. Playbooks
family net +3 opus / 0 grok / +4 sonnet / −3 gpt of 57; the drops (`spec-from-guarantee` gpt 3→0, `preflight-not-real-run`
grok 3→1, `rollback-restores-previous` opus 2→0) are a shift to direct `playbook_edit` authoring over delegation and the
harness's reject-all approval stance, not this plugin's wake change (`followthrough.async-fail-followup` seeds now, still
0/3 under the stance). The `break-*` spec-seed 404 is open. Full numbers: dojoP `plans/0003_fix18fails/execution_summary.md`.
Merge: `fix18fails` → `v2-runtime` (local, no push until OK).
