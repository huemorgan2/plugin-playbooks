# 032 — Phase 01: v2 contract doc, static checker and format sniff — execution summary

Status: done (independent verifier green, 0 fix rounds)

## Ran
- Date: 2026-09-07. Repo `luna-plugins/plugins/plugin-playbooks`, branch `v2-runtime`.
  Python via `.venv/bin/python` (no install); ruff via `uvx ruff` (nothing installed into the repo).
- HEAD before: `efe0869` (phase file cited 18b9ebe; two docs-only commits `081b58d` phase 00 summary and
  `efe0869` phases 06-11 fix-ups had landed on top — code identical to 18b9ebe).
- HEAD after: `0f61ba6` — one commit on top of efe0869, not pushed
  (`git ls-remote --heads origin v2-runtime` → empty).
- Commit: `0f61ba6 032 phase 01: docs/v2.md, v2 checker, format sniff` — 5 new files, 2451 insertions, 0
  deletions: `docs/v2.md` (368), `plugin_playbooks/v2/__init__.py` (47), `plugin_playbooks/v2/checker.py`
  (1195), `tests/test_v2_checker.py` (688), `tests/test_v2_contract_doc.py` (153). Staged by explicit path;
  `uv.lock`, `.env`, `plans/` not in the commit; `validation.py` untouched; `Co-Authored-By` trailer present.
- Step 1: `git status --short` = ` M uv.lock` only. Baseline (warm) `.venv/bin/python -m pytest -q` →
  `7 failed, 378 passed` = phase 00's count. One cold run showed 8 failed — the known timing flake
  `tests/test_delegation.py::test_slow_path_returns_running_then_status_polls_done` (phase 00 summary);
  green on every warm run; not touched.
- Step 2: `plugin_playbooks/v2/__init__.py` with exactly the listed constants (`MAX_EFFECTS=200`, `FORMATS`,
  `AVAILABLE_EFFECTS` (9), `UNAVAILABLE_EFFECTS` (`wait_event`, `sleep`), `DEFAULT_FEATURES=frozenset()`,
  `CTX_EXCEPTIONS` (8), `CTX_UNCATCHABLE` (3), `DEFAULT_TIMEOUTS`, `APPROVE_RESULT_KEYS`).
  `tests/test_smoke_import.py` + `tests/test_no_spec_feature.py` → 17 passed.
- Step 3: `docs/v2.md` — `grep -c '^## ' docs/v2.md` = 12, headings `## 1. Shape` … `## 12. Constants` in the
  prescribed order (lines 12, 76, 115, 129, 149, 172, 203, 251, 306, 323, 345, 354). §2 table: 11
  `| \`ctx.x(\`` rows (9 available + `wait_event`/`sleep` "not available in this version"); `ctx.approve`
  Returns cell = the 4 `APPROVE_RESULT_KEYS`, no `approval_id` (the only other mention is the §3 sentence
  saying it is never an effect-result key). Exactly two ```python blocks: the master §2 example verbatim
  (difflib against luna-fixer `plans/2026-09-06-fix-playbooks/PLAN.md` §2: identical) and the queue example
  (`while queue:` / `.pop(0)` / `try: … except ctx.ToolError:` / `raise` / `ctx.gather`). §8 rule table = 34
  codes = `RULES`. §12 carries `MAX_EFFECTS = 200` and
  `DEFAULT_TIMEOUTS = {"tool": 120, "llm": 300, "agent": 900, "subtask": None}`.
  `grep -nE '\bTests\b|\bspecs?\b|playbook_spec' docs/v2.md plugin_playbooks/v2/*.py` → 0 hits.
- Step 4: `tests/test_v2_checker.py` written first (red), then `plugin_playbooks/v2/checker.py`. One bug on the
  first run (unavailable effects not routed to the effect pass) fixed → 82 passed.
- Step 5: `tests/test_v2_contract_doc.py` — two test-side fixes (table cell index; sentence-level
  "cannot be caught" check) → 8 passed.
- Step 6: full suite `.venv/bin/python -m pytest -q` → `7 failed, 468 passed, 5 warnings in 8.83s`
  (475 collected = 385 baseline + 90 new). `uvx ruff check --select F401 plugin_playbooks tests` →
  `All checks passed!`.
- Step 7: commit `0f61ba6`; `git status --short` = ` M uv.lock` only.
- Working tree after: ` M uv.lock` (left as found, never staged).

## Results
- `tests/test_v2_checker.py` — 82 passed:
  - `test_master_example_passes_clean`: `issues == []` with and without `tool_names={"fetch_list","send_message"}`;
    `summary["tools"] == ["fetch_list", "send_message"]`; call-site ids `["rows", "s", "approve", "send_message"]`.
  - `test_queue_example_passes_clean`: `issues == []` (the dojop/01 grader constructs are admissible).
  - `test_rule[<code>]` parametrized over `sorted(RULES)` (34 codes) — each asserts the code, `severity ==
    RULES[code].severity`, non-empty `message`/`expected`/`example_fix`, `source_line == code.splitlines()[line-1]`,
    8-key `to_dict()`; plus 24 fixed-point tests (`test_rule_v2_*`, `test_rule_ported_lints_codes_and_severities`).
    Fixed points: R10 message "not available in this version" for `ctx.sleep`, `ctx.wait_event`, `asyncio.sleep`,
    `time.sleep`; with `features={"wait_event"}` R10 absent and `v2-unknown-kwarg` fires on a missing `timeout=`;
    `ctx.sleep` still rejected; the four ported codes present, `monolithic-playbook` the only error.
  - `test_all_issues_at_once`: all seven codes in one result, sorted by line, `ok is False`.
  - `test_every_issue_has_example_fix`: every issue in the corpus carries `expected` + `example_fix`;
    codes seen == `set(RULES)` (incl. `v2-format-mismatch`/`v2-format-unknown` via `resolve_format`, R17 with
    `tool_names=`, R21 with `inputs_schema=`).
  - `test_line_numbers_match_saved_code`: `print` on line 9 → `line == 9`; `filename == "playbook:t@v3"`;
    `compile(...).co_filename == result.filename`; `run` `co_firstlineno == 8`.
  - `test_syntax_error_is_the_only_issue` (+ `test_rule_v2_syntax_position_and_only_issue`: `(2, 8)`).
  - `test_summary_call_sites`: ids `["first", "rows", "touch", "child", "approve", "rows_2", "log", "x"]`; no `#`;
    R16b at the collision line; `loop_depth`, `in_try`, `inputs_read`, `subtasks`, `tools` asserted.
  - `test_sniff_format` ×7, `test_resolve_format_table` ×8 (all rows of the plan's table; issues at `(1, 0)`,
    severity `error`).
- `tests/test_v2_contract_doc.py` — 8 passed: `test_doc_exists_with_sections`,
  `test_every_doc_effect_has_a_checker_rule_or_is_accepted` (11 rows == `AVAILABLE_EFFECTS | UNAVAILABLE_EFFECTS`),
  `test_doc_exceptions_match_checker`, `test_doc_rule_table_matches_RULES`, `test_doc_constants`,
  `test_doc_approve_result_keys_match_constant`, `test_doc_examples_pass_clean` (blocks byte-identical to the
  checker test fixtures `EXAMPLE` / `QUEUE_EXAMPLE`), `test_no_spec_feature_tokens` (reuses the guard's own
  `_FEATURE_RE` / `_BARE_SPEC_RE`).
- Existing-suite gate: `7 failed, 468 passed`; the 7 red = exactly `tests/test_repro_fixplaybooks_lifecycle.py` ×3
  + `tests/test_repro_fixplaybooks_runtime.py` ×4 — none flipped. `tests/test_manifest_drift.py` untouched, green
  (stamps 0.47.0 at `pyproject.toml:3`, `plugin_playbooks/luna-plugin.toml:2`, `__init__.py:616`).
- Verifier (independent, HEAD 0f61ba6): all of the above re-run and green; no skip/xfail markers in the new tests;
  no assertion removed; secret-pattern grep over the diff → 0 hits.

## Deviations from this plan
- Step 8's summary and later-phase revisions were done by the orchestrator's docs pass (this document), not in the
  executor run.
- HEAD before was `efe0869`, not the cited 18b9ebe (docs-only commits on top; code identical). All cited
  `validation.py` anchors (`_prompt_markers` :128-142, `_DEEP_COLLECTION_REF` :80-83, marker tables :89-118,
  lints :693-827) matched; no other line-number lookups were needed.
- Per-rule tests are one parametrized `test_rule[<code>]` plus named fixed-point tests rather than
  `test_rule_<code>` functions; `test_every_issue_has_example_fix` is one loop over the corpus rather than
  parametrized. Assertions are the plan's.
- `test_doc_exceptions_match_checker` checks the "cannot be caught" sentence, not the whole §4 paragraph, because
  that paragraph also names `ctx.EffectError` as the fix.
- `RULES` has 34 codes (28 `v2-*` rules incl. R16b, 4 ported lints, 2 format codes) — the plan's "R1-R30" list
  counts rule groups, not codes.
- No version bump (stamps stay 0.47.0; batches into plugin/04's 0.48.0).
- `checker.py` is 1195 lines (message/example strings); no functional deviation.

## Learned
- Public surface (plugin/02, /04, /05, /10 build on it): `plugin_playbooks.v2.checker` exports `RULES` (34),
  `CheckIssue` (8 keys incl. `severity`), `CheckResult(issues, summary, filename, ok)`, `stable_filename`,
  `sniff_format`, `resolve_format`, `check(code, *, name, version, inputs_schema=None, tool_names=None,
  features=DEFAULT_FEATURES)`; `summary` keys `format, tools, subtasks, call_sites, inputs_read, imports`;
  `call_sites[]` keys `id, kind, line, col, tool, playbook, loop_depth, in_try`. Thresholds `MAX_CALL_SITES = 40`,
  `NESTED_LOOP_DEPTH = 2` (checker.py:156-157).
- Call-site id rule as implemented: `_id=` literal → assignment-target `Name` → literal tool/playbook name
  (`ctx.tool`/`ctx.subtask`) → `<kind>`; collision → `<id>_2`, `<id>_3` + R16b warning; never `#`. Hence
  `rows = await ctx.tool("fetch")` is `rows` (per-occurrence key `rows#1`), an unassigned
  `await ctx.tool("fetch")` is `fetch` (`fetch#1`).
- Decisions on the Risks 1-5 assumptions, all taken as written: `severity` in the issue shape; whitelist/thresholds
  as listed; `compound-leaf` ported as the fourth lint; `context-economy` assigned that code; `approve(show=)`
  required; `wait_event` `timeout=` required behind `features` (plugin/07 flips `DEFAULT_FEATURES` only).
  Extra rulings: `ctx.tool(name, **kw)` splat allowed (keyword-only args), splat rejected on other kinds;
  `inputs.get/keys/items/...` exempt from R20; R12's attribute form skips receivers that are imported/whitelisted
  modules (`math.log` is clean).
- Subtask failure class is `ctx.SubtaskFailed` (an `EffectError` subclass, `CTX_EXCEPTIONS`): plugin/03's child
  failure must raise it, not bare `EffectError` (`except ctx.EffectError` still catches it).
- `ctx.approve` result key set is `APPROVE_RESULT_KEYS = {approved, request_id, reason, decided_by}` in
  `plugin_playbooks/v2/__init__.py`; plugin/03/05/07 return exactly it (dry adds `dry`).
- The queue example is byte-identical in `docs/v2.md` and `tests/test_v2_checker.py::QUEUE_EXAMPLE`; plugin/05's
  skill copies the doc's two blocks and its byte-compare test can use the same fixtures.
- Full-suite count for later baselines: 475 collected (468 green + 7 red repro pins).

## Revised
- `phases/02-shim-and-segment-loop/PLAN.md`: header cites phase 01's HEAD 0f61ba6 and the checker's public
  surface (`check()` signature, `summary`/`call_sites` keys, RULES count); id rule already matched.
- `phases/03-llm-agent-subtask-gather-approve/PLAN.md`: header cites 0f61ba6; subtask child failure →
  `ctx.SubtaskFailed` (Scope, Step 6, exit test 10); approve result pinned to `APPROVE_RESULT_KEYS` with the
  `set(entry["result"]) == APPROVE_RESULT_KEYS` assertion added to exit test 12.
- `phases/04-lifecycle-corrections/PLAN.md`: Risks 8 — phase 03's plan now cites 0.47.0, so the stamp is
  0.47.0 after phases 00-03 unless a summary says otherwise; note that the checker issue `to_dict()` already
  carries `severity` for the `errors`/`warnings` split.
- `phases/05-dry-run-skill-and-go-no-go/PLAN.md`: `resolve_stub` id rule corrected (tool/playbook literal
  before `<kind>`; `_2` suffix, never `<kind>#<n>` as an id); dry `approve` answer = `APPROVE_RESULT_KEYS` + `dry`;
  Risks 13's `tool#1#1` collision marked void under the phase 01 rule.
- Phase files 07 and 10 (also named in Step 8) are outside this docs pass's scope (02-05); their `fetch#1` /
  `step-x#2` expectations still need the phase 01 id rule applied when they are next revised.

## Post-verification fix-up (2026-09-08, found by dojop/01 part B)

The first bench smoke on 0.49.0 was void: luna's loader (`luna/plugins/loader.py::_import_module`) imports
image-set/managed plugins under the synthetic name `luna_plugin_plugin_playbooks`, so the absolute
`from plugin_playbooks.v2 import …` / `from plugin_playbooks.validation import …` lines added by 0f61ba6 in
`plugin_playbooks/v2/checker.py` raised `ModuleNotFoundError` at `on_load` and luna fell back to the managed
0.46.0 copy (`plugins.winner_load_failed … No module named 'plugin_playbooks'`). Invisible to this repo's pytest
because the package is importable as `plugin_playbooks` from the repo root.

Fix: relative imports in `v2/checker.py`; new guard `tests/test_loader_style_import.py` — loads the package in
a subprocess under the synthetic name with the real name blocked and imports every submodule (red on the old
checker: agent_tools, runner, triggers, v2.checker all failed; green after), plus a static twin that rejects any
`from plugin_playbooks` / `import plugin_playbooks` line. Also confirmed through luna's real `_import_module` in
luna's venv (`loaded as luna_plugin_plugin_playbooks`, `.agent_tools`, `.v2.checker`, `.runner`, `.triggers` ok).
Version stays 0.49.0 (nothing published yet).

Re-check at HEAD `9fd01f2` (2026-09-08): the phase's five files are byte-identical to `0f61ba6` except the two
import lines in `v2/checker.py`; `.venv/bin/python -m pytest -q tests/test_v2_checker.py
tests/test_v2_contract_doc.py tests/test_loader_style_import.py` → `92 passed` (82 + 8 + 2). Independent
verifier at `0f61ba6` (detached worktree, removed afterwards): 82 + 8 passed, full suite `7 failed, 468 passed`,
0 fix rounds.
