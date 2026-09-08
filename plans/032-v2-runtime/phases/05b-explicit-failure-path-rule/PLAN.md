# 032 — Phase 05b: the explicit-failure-path rule in the v2 skill (M1 re-plan 2)

Status: approved — luna-fixer re-plan after the M1 STOP RULE fired a second time on dojoP run 0060 (luna-fixer `plans/2026-09-06-fix-playbooks/phases/M1-authoring-surface/stop-report.md` "Second stop"); covered by the master plan's standing approval (execution go 2026-09-07); execution not started

Master: §2 Prompt surface (the v2 skill is the authoring surface under measurement); §3 P1 STOP RULE; master phase M1 (re-plan inside M1; M2 still not built)

Repo / branch: plugin-playbooks v2-runtime (HEAD 8544ed7 at writing: code 92cb5f6 + loader fix 9fd01f2, stamp 0.49.0, suite 571 green + 7 intended red repro pins)

Depends on: plugin/05 (`V2_SKILL_BODY`, `tests/test_v2_skill.py`, `docs/v2.md`); dojop/01b (run 0060 — the finding this phase answers)

Unblocks: dojop/01c (runs the bench on this build); through it plugin/06 and luna-fixer M2 (only on `VERDICT: go`)

## Why (run 0060, luna-fixer stop-report "Second stop")
`editing-cross-cutting-v3` scored 2/5. `ctx\.gather\(` and `entries` landed 5/5 (the 0059 finding is closed); the only red
check was `\braise\b|\btry:` on turn 2 in trials 1-3. The ask was "if any file write fails, the playbook must stop with a
clear error instead of finishing as if nothing happened". Trials 4-5 wrote `try: … except ctx.ToolError as e: raise
ValueError(…)`; trials 1-3 left the gather bare and (trial 1) replied that a failure "will propagate immediately and halt
the run" — which is true (`docs/v2.md` §2: an uncaught effect error fails the run). The skill teaches the mechanism
(`Catch failures as ctx.ToolError etc.`, the second example's `try/except … raise ValueError`) but carries no rule for
WHEN an explicit failure path is required. The master's error contract wants a run to fail with a message naming what
failed, not the raw tool error; the skill does not say so. This is a skill-surface gap, not a runtime defect (every edit
`validated: true`; checker, shim, loop, dry run unchanged by the outcome).

The second 0060 finding — `authoring-stateful-queue-v2` trial 1 delegated through `playbook_agent`, whose code no
`tool_args_regex` can see — is NOT answered here. The delegation default (plans/013, reinstated by plans/020; v2 prompt in
phase 11) is a product decision and stays; the bench grades the saved candidate instead (dojop/01c).

## Goal
The v2 skill states the rule: when the owner wants the run to stop on a failed effect, the playbook carries an explicit
failure path — `try:` around the effect(s), `except ctx.ToolError as e:` re-raised as an error naming what failed — and
the reply says so. Same sentence in `docs/v2.md` (the skill is written from the doc). Stamp 0.50.0. Nothing else changes.

## Scope — changes
1. `plugin_playbooks/v2/skill.py` — `V2_SKILL_BODY`: after the `Options: … is the only stdout.` paragraph add one
   paragraph (≤ 330 bytes; the body is 5737 of `V2_SKILL_MAX_BYTES` 6144):
   ```
   FAILURE PATH: when the owner wants the run to stop on a failed effect,
   write it explicitly — `try:` around the effect(s), `except ctx.ToolError
   as e: raise ValueError(f"<what> failed: {e}")` naming the item — and say
   so in the reply. An uncaught error also fails the run, but without a
   message naming what failed; the explicit raise is what was asked for.
   ```
   Wording may be tightened to fit the bound; the words `FAILURE PATH`, `try:`, `except ctx.ToolError`, `raise` and
   "naming" must survive. The two ```python blocks stay byte-identical to `docs/v2.md` (`test_v2_skill.py::_blocks`).
   The `_DELEGATION_SKILL_BODY` and `_AUTHORING_SKILL_BODY` are NOT touched.
2. `docs/v2.md` — the same rule as one bullet in §2 next to the "A CAUGHT effect failure is journaled `failed_handled`"
   bullet (line ~107), so the doc and the skill agree.
3. `tests/test_v2_skill.py` — new `test_failure_path_rule`: the skill body contains `FAILURE PATH`, `except ctx.ToolError`
   and `raise`, and the normalized rule sentence appears in `docs/v2.md`; `test_size_bound` keeps holding.
   New `test_failure_path_rule_is_checkable`: a minimal playbook with the recommended shape (`try:` around a `ctx.gather`
   of `ctx.tool("file_write", …)` calls, `except ctx.ToolError as e: raise ValueError(...)`) passes `checker.check` with
   `ok=True` (a warning is allowed, an error is not).
4. Stamp 0.50.0 in `pyproject.toml`, `plugin_playbooks/luna-plugin.toml`, `plugin_playbooks/__init__.py`
   (`PluginManifest(version=…)`) — the three must agree (`tests/test_manifest_drift.py` or its equivalent stays green).
5. Rebuild the bench image-set `~/.luna/bench-set-0002-01/plugin_playbooks` from this working tree at the phase's final
   commit (delete the stale dir first; `__pycache__` removed; `grep -r 'from plugin_playbooks'` = 0 hits; manifest
   0.50.0). dojop/01c's version proof is `plugin_versions["plugin-playbooks"] == "0.50.0"`.

## Not in this phase
Any change to the delegation skill or its trigger, `_AUTHORING_SKILL_BODY`, the checker, shim, loop, dry run, tools;
the v2 delegate prompt (phase 11); any luna change; any dojoP change (dojop/01c); a `vaselin-*` side-load (M6).

## Steps
1. Edit skill + doc + tests + stamps (Scope 1-4). Relative imports only (`tests/test_loader_style_import.py`).
2. `.venv/bin/python -m pytest -p no:cacheprovider -q tests` — expected: previous 571 green + the 2 new tests, the same
   7 intended-red repro pins and nothing else red (list them; compare against plugin/05's summary).
3. Commit by explicit path (skill.py, docs/v2.md, tests/test_v2_skill.py, pyproject.toml, luna-plugin.toml, __init__.py,
   this phase folder): `032/05b: explicit failure-path rule in the v2 skill — 0.50.0` + trailer. Never `uv.lock`, never push.
4. Scope 5 (bench set rebuild). Record the rebuilt dir's manifest version and the grep result in the summary.
5. `execution_summary.md` here (template below).

## Exit tests
1. `test_size_bound`, `_blocks` byte-equality, `test_failure_path_rule`, `test_failure_path_rule_is_checkable` green;
   suite: no new red versus plugin/05's 7 pins; `test_loader_style_import.py` green.
2. `grep -c "FAILURE PATH" plugin_playbooks/v2/skill.py docs/v2.md` → 1 each; `git diff --stat` of the phase commit lists
   only the files in Steps 3.
3. All three stamps read 0.50.0; `~/.luna/bench-set-0002-01/plugin_playbooks/luna-plugin.toml` reads 0.50.0.
4. The bench verdict (dojop/01c) is appended to this summary after the run; `VERDICT: go` lifts phase 06's BLOCKED
   Depends line; stop leaves it.

## Risks and open questions
- R1 The rule may not move trials that judge the explicit raise redundant; if editing-v3 still fails `\braise\b|\btry:`
  with the rule present, the owner decides the grader over-specifies an already-satisfied ask → dojoP retires v3 and
  replaces (v4 asks for "raise an error naming the entry that failed"); never a task edit, never a second skill patch
  without a new re-plan.
- R2 Size bound: the body must stay ≤ 6144 B; tighten wording, do not raise the bound.

## Execution summary (→ `execution_summary.md` here)
```
# 032/05b — execution summary
Date / operator:
## Ran
- commit(s):            pushed: no
- suite:                (passed / red pins list / new tests)
- stamps:               0.50.0 × 3
- bench set rebuilt:    dir, manifest version, 'from plugin_playbooks' hits
## Deviations from this plan
## Learned
## Bench verdict (appended by dojop/01c)
```
