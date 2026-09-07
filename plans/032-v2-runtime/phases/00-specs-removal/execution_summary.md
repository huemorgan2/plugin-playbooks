# 032 — Phase 00: Specs (Tests tab) removal — execution summary

Status: done (independent verifier green after 1 fix round)

## Ran
- Date: 2026-09-07. Repo `luna-plugins/plugins/plugin-playbooks`, branch `v2-runtime`.
  Python via `.venv/bin/python` (no install); UI via `cd ui-src && npm ci && npm test && npm run build`.
- HEAD before: `c8db2c8` (plan-file fix-ups on top of 5306f7f / code HEAD 8c31a60).
- HEAD after: `18b9ebe` — one commit on top of c8db2c8 (amended in place after fix round 1), not pushed
  (`git ls-remote --heads origin v2-runtime` → empty).
- Commit: `18b9ebe 0.47.0: remove the specs (Tests tab) feature — master §2 Specs removal, owner 2026-09-07`
  — 54 files changed, 1202 insertions(+), 3361 deletions(-); `Co-Authored-By` trailer present;
  `git show --name-only HEAD | grep -iE "^\.env|uv\.lock|^plans/"` → none; secret-pattern grep over
  `git show HEAD` → 0 hits.
- Baseline at c8db2c8: `pytest -q` → `7 failed, 403 passed` (the 7 = repro tests, red by design).
- Order of operations 1-6 from `specs-removal.md` followed (tests first, then package, DB helper, UI +
  manifest + stamps, docs + guard + lint + commit). Every plan line number had shifted; edits were located
  by symbol/text (`_resolve_target`, `_drop_legacy_indexes`, `specs_gate`,
  `SkillDef(name="playbook-authoring")`, VersionsTab `VIEWS`, …).
- Fix round 1 (verifier findings): `uvx ruff check --select F401 --fix plugin_playbooks tests` removed the
  5 pre-existing unused imports (`pblang/compiler.py` `keyword`, `dataclasses.field`; `runner.py` `time`;
  `testing.py` `dataclasses.field`; `tests/test_repro_fixplaybooks_lifecycle.py` `pytest`); guard
  `tests/test_no_spec_feature.py` strengthened (ast-scoped `_drop_spec_remnants` allowlist,
  `publish_require_specs` token, explicit `_COLUMN_MIGRATIONS`/`_LEGACY_INDEXES` assertion,
  `playbook_list_available_triggers` comment corrected). Staged by explicit path,
  `git commit --amend --no-edit` (0fd4ae9 → 18b9ebe).
- Final: `.venv/bin/python -m pytest -q` → `7 failed, 378 passed, 5 warnings in 7.80s`.
- `uvx ruff check --select F401 plugin_playbooks tests` → `All checks passed!`.
- Working tree after commit: ` M uv.lock` (left as found, never staged) + the plans/032 PLAN.md files
  modified by the parallel docs pass (untouched).

## Results
- Full suite: `7 failed, 378 passed, 5 warnings in 7.80s`. The 7 red = exactly
  `tests/test_repro_fixplaybooks_lifecycle.py` ×3 + `tests/test_repro_fixplaybooks_runtime.py` ×4;
  none flipped green (expected). `tests/test_specs.py`, `tests/test_versioned_specs.py` → `ls: No such file`.
  Collected total 385 (378 + 7); the 8c31a60 figure 410 no longer applies.
  Note: one cold-interpreter run (27.9 s) showed
  `tests/test_delegation.py::test_slow_path_returns_running_then_status_polls_done` red (timing,
  `wait_seconds=0.05`); warm runs ×3 (7.6-7.8 s) green — body unchanged by this phase; not fixed.
- Relocated dry-run tests `tests/test_plan026_navigable_dry_stubs.py`:
  `test_dry_run_stub_by_step_id_and_tool_name`, `test_dry_run_stubs_agent_and_llm_steps`
  (`trace[0].output == {"label":"urgent"}`, `trace[1].resolved_args == {"v":"urgent"}`),
  `test_loop_over_unstubbed_dry_output_iterates_zero_times` (iterations 0, results []) — PASSED on
  `_bare_runner({"t","send_chat_message"})`.
- Fixture importers: `tests/test_publish_settings.py` 5 passed (PATCH `{}` → 400, `require_run` persists,
  404 unknown name, `publish_require_run is False`, both never-blocks, `candidate_tool_publish_run_gate_off`);
  `tests/test_tool_timeouts.py` 1 passed (`timeout_seconds >= 300`);
  `grep -rln "test_versioned_specs\|from test_specs" tests/` → empty.
- `tests/test_manifest_drift.py` → `4 passed` (tools = 25, tables = 10; `0.47.0` at `pyproject.toml:3`,
  `plugin_playbooks/luna-plugin.toml:2`, `plugin_playbooks/__init__.py:616`).
- `tests/test_pblang.py::test_skill_examples_compile` → PASSED.
- Drop helper (`tests/test_no_spec_feature.py`): `test_drop_spec_remnants_removes_table_and_column`
  (caplog contains `playbook_specs (2 rows)` and `0.47.0`), `test_drop_spec_remnants_is_idempotent`,
  `test_fresh_db_never_creates_spec_remnants` — PASSED. Helper `_drop_spec_remnants` at
  `__init__.py:83-124`, called in `on_load` after `_drop_legacy_indexes` and before the index-create loop,
  own try/except (`__init__.py:716-724`). PG rehearsal: deferred (owner M0 touchpoint on a `vaselin-*`
  agent; export path to be recorded when done — nothing exported in this run).
- Publish gate list: `tests/test_candidate_flow.py:255-258` asserts
  `["static_validation", "test_run", "probes"]` + `all(g["ok"])`; file 18 passed. Guard item 4:
  `"require_specs"` not in `set_autonomy` properties; raw `require_specs=True` → `TypeError` — PASSED.
- Guard `tests/test_no_spec_feature.py` → `15 passed` (items 1-6). Token list `_FEATURE_TOKENS`
  (:56-63) = plan list + `publish_require_specs`; `\bTests\b` allowlist = `reference.py:110` Jinja line only
  (`grep -rnE "\bTests\b"` over package + README + vision → that one hit); bare `\bspecs?\b` allowlist
  documented in the module docstring, `__init__.py` entry ast-scoped to the `_drop_spec_remnants` body.
  `prompt_always == {playbook_set_autonomy, playbook_run_candidate} ⊆ _GATED_TOOLS`;
  skill tools ⊆ `AUTHORING_TOOLS`; `_COLUMN_MIGRATIONS`/`_LEGACY_INDEXES` name no spec remnant.
  Negative check done: re-adding a `publish_require_specs` migration tuple fails the guard.
- Step-2 proof: `grep -rn "_spec_target" plugin_playbooks/` → 0 hits; `tests/test_probes.py` 22 passed.
- Step-3 proof: literal `grep -rn "playbook_specs\|require_specs" plugin_playbooks/*.py` → 9 hits, all
  inside `_drop_spec_remnants` (`__init__.py:85-123`) — the only coherent reading of Step 3 vs Step 4.
- UI: `npm ci` (192 packages) → `npm test` → `Test Files 17 passed (17), Tests 126 passed (126)`;
  `npm run build` (tsc -b + vite) → `plugin_playbooks/ui/assets/index-DxyKmHkO.js` +
  `index-C5Bp-kfQ.css`, `ui/index.html:7-8` updated, old `index-BqhDbTui.js` + `index-BgLNZxTK.css`
  removed; verifier rebuild reproduced the identical pair (deterministic).
  `TestsTab.tsx` → `ConnectionsTab.tsx` (probes only), `__tests__/ConnectionsTab.test.tsx` replaces
  `TestsTab.test.tsx`; VersionsTab view id `connections`; no `specsLabel`/`specsHeadline`.
- Bundle grep: `grep -c "require_specs\|/specs\|tests-header\|version-specs\|No tests yet"
  plugin_playbooks/ui/assets/*.js` → 0.
- `uvx ruff check --select F401 plugin_playbooks tests` → `All checks passed!`.
- Cross-repo checks: luna/00 and dojop/00 — not run in this phase (owned by those phases in the same M0
  run). Shas at the time of writing this summary: luna `fix-playbooks` 90e0a60, dojoP `main` f325b03.
  VERDICT: plugin side green; cross-repo verdicts recorded by luna/00 and dojop/00.

## Deviations from this plan
1. Step 0 `uv.lock` reconciliation NOT done: standing rule wins — ` M uv.lock` left exactly as found,
   never staged, never discarded. `git status --short` is therefore not empty (uv.lock + docs-pass plan
   files). After the 0.47.0 bump the lock's root version is stale again (Risk 1); no `uv lock` was run.
2. HEAD at start was c8db2c8 (docs-only) rather than 5306f7f; code identical.
3. Guard bare-word allowlist is wider than the checklist's lines: `__init__.py` `_drop_spec_remnants`
   (ast-scoped) and pre-existing non-feature "spec" hits in `reference.py`, `agent_tools.py`
   (`hand it the spec`/`attach the spec`), `runner.py`/`testing.py` `_stub_for_type(spec)`,
   `pblang/compiler.py` "format specs". All present at c8db2c8 and documented in the guard docstring.
4. Pre-existing drift found, NOT fixed (out of scope): `playbook_list_available_triggers` is in
   `delegation._PHASE_BY_TOOL`, the `playbook-authoring` SkillDef tools and `AUTHORING_TOOLS`, and is
   registered via `_register_tool` in `on_load` (`__init__.py:959`), but it is not a `build_tools`
   ToolDef and `luna-plugin.toml` does not declare it (tool count 25 excludes it). The guard tolerates
   exactly that one name (`tests/test_no_spec_feature.py:398-402`).
5. `publish.py` test_run refusal reworded to "Dry runs are not run evidence — they are simulations with
   tools stubbed." (not the checklist sentence) so `tests/test_build_operate.py`'s `"simulations"` pin
   holds without weakening the test.
6. `ui-src/tsconfig.tsbuildinfo` (tracked) regenerated by the mandated build and committed.
7. Exit test "ruff F401 → All checks passed!" was false at baseline (5 pre-existing findings). Resolved
   to the literal wording by deleting the 5 unused imports, touching `pblang/compiler.py`, `testing.py`
   and `tests/test_repro_fixplaybooks_lifecycle.py` (`import pytest` only; body and red status unchanged)
   outside the phase edit list. Zero behaviour change. Revert those hunks if the owner prefers
   "no new F401 vs baseline".
8. Plan Step 3 proof (0 hits for `playbook_specs|require_specs`) contradicts Step 4; implemented as the
   ast-scoped function-body allowlist described above. The plan text should exempt `_drop_spec_remnants`.
9. All plan line numbers had shifted; every edit located by symbol.

## Learned
- Stamps: 0.47.0 at `pyproject.toml:3`, `plugin_playbooks/luna-plugin.toml:2`,
  `plugin_playbooks/__init__.py:616` (was :624). Manifest `tools = 25`, `tables = 10`.
- Suite baseline for later phases: 385 collected = 378 green + 7 red repro pins.
- `agent_tools.py` is now 2750 lines; anchors at 18b9ebe: `_set_autonomy` :833, `_resolve_target` :1529,
  `_dry_run` :1564-1603 (inputs/stubs JSON parse :1568-1581, `_resolve_target` call :1592-1595,
  `runner.dry_run(target, inputs=input_data, stubs=stub_data)` :1598, `tested_version`/`is_candidate`
  :1599-1603, `playbook_dry_run` description :1610-1619, `stubs` schema :1625-1632), `_preflight` :2682.
- `__init__.py` (1102 lines): `_COLUMN_MIGRATIONS` :19, `_LEGACY_INDEXES` :40 (empty),
  `_drop_legacy_indexes` :66, `_drop_spec_remnants` :83, `manifest = PluginManifest(` :612, `skills=[`
  :631-680 (v1 SkillDef :632, tools list :644-658 — 13 tools; delegation SkillDef :663),
  `AUTHORING_TOOLS` :866-882, `_register_tool` :893, `playbook_list_available_triggers` registration :959.
- `runner.dry_run` is at `runner.py:556`.
- Guard token list for plugin/01 to mirror: `tests/test_no_spec_feature.py::_FEATURE_TOKENS` (:56-63,
  18 tokens incl. `publish_require_specs`, `Tests`, `all specs`) + bare `\bspecs?\b` with the file-keyed
  allowlist (:72-80). The guard greps the package, skill bodies, ToolDefs, delegate prompt, `card.py`,
  README, vision and the built bundle — not `docs/`; plugin/01's own token test still covers `docs/v2.md`.
- `git status --short` will not be empty for later phases: ` M uv.lock` persists by standing rule, and
  plans/032 files may show as modified by a parallel docs pass.
- Ruff F401 is now clean at 18b9ebe; later phases can require "All checks passed!" literally.
- `uvx ruff` works from the uv cache without installing into `.venv`.

## Revised
- `phases/01-contract-and-checker/PLAN.md`: stamp confirmed (0.47.0, `__init__.py:616`); precondition
  "git status clean" qualified (uv.lock + docs-pass plan files expected); suite count 378/7 named; Risk 7
  re-pointed at the actual guard token list.
- `phases/02-shim-and-segment-loop/PLAN.md`: HEAD line notes 18b9ebe and that agent_tools/__init__ lines
  shifted; stamp line records 0.47.0 at `__init__.py:616`.
- `phases/03-llm-agent-subtask-gather-approve/PLAN.md`: version 0.46.0 → 0.47.0 at HEAD 18b9ebe;
  suite figures 378 green + 7 red; `__init__.py:616`.
- `phases/04-lifecycle-corrections/PLAN.md`: `__init__.py:624` → :616; `_resolve_target`/`_dry_run`/
  `_preflight`/`_set_autonomy` re-cited at 18b9ebe; Risk 8 confirmed 0.47.0.
- `phases/05-dry-run-skill-and-go-no-go/PLAN.md`: `_dry_run` anchors re-cited (the inline version
  resolution is now one `_resolve_target` call); SkillDef/AUTHORING_TOOLS/_register_tool anchors re-cited;
  note that `playbook_list_available_triggers` is on_load-registered and absent from the manifest
  `[[tools]]` (tool count 25 excludes it).
- Repo `PLAN.md`: Risks 3 (0.47.0 stamped) and 5 (`uv.lock` was not reconciled; still pending).
