Status: approved
Approval: Roy, 2026-09-16, in session (see master). Covers changes and merge to `v2-runtime` (the live line; main is at 0.46.0); publishing waits for Roy's OK.

# 035 — fix18fails (plugin-playbooks part)

Master plan: dojoP `plans/0003_fix18fails/PLAN.md` (thesis `idea_fix.md`, test changes `research/agent-rigor-and-scope/results.md` §0003). This mirror lists only what changes in THIS repo; evidence, diagnosis, date validation and gates are in the master.

Branch `fix18fails` from `v2-runtime` `ab77466` (0.57.1). Version 0.57.2; stamps: `pyproject.toml:3`, `plugin_playbooks/luna-plugin.toml:2`, `plugin_playbooks/__init__.py:736`.

## Phase 1 (0.57.2) — `plugin_playbooks/wake.py`
- `:50-56` caps: `_WAKE_TOKEN_BUDGET` 200_000 → 1_500_000, `_WAKE_MAX_TURNS` 12 → 20 (the meter is cumulative input+output over the turn's requests; the 900 s timeout is the real limiter).
- `:290-296` failure text: add `error_type`, the traceback (capped), the failing step id and its inputs (from the step rows), and end with "Then continue what the original request asked for — fix and rerun if the request said so; report honestly what you did." Keep "do NOT fabricate results".
- `:320-335` and `:250-265`: inspect the `send()` result; `aborted`/`error` → `log.warning("run_wake.moment_aborted", reason)`, not `run_wake.moment`.

## Tests
Failure text contents; aborted send logged. Existing wake tests stay green.
