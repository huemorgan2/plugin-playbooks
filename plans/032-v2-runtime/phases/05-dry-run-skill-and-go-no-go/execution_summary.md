# 032 — Phase 05: Dry run on the same loop, the v2 skill, and the bench go/no-go — execution summary

Status: done (code, Steps 1-8 and 12; independent verifier green, 0 fix rounds). Stamps 0.48.0 → 0.49.0. Bench Steps 9-11 ran in dojoP run 0059 — verdict stop (see Results, "Bench"); plugin/06 stays blocked until luna-fixer re-plans.

## Ran
- Date: 2026-09-08 (commit 01:38 +0300). Repo `luna-plugins/plugins/plugin-playbooks`, branch `v2-runtime`. HEAD before `2227a83` (plugin/04 summary), HEAD after `92cb5f6` — one commit "0.49.0 (plans/032 phase 05): dry run on the segment loop + v2 skill", not pushed (`v2-runtime` has no remote tracking branch; origin has only `main`). luna and dojoP untouched. ` M uv.lock` left as found, not staged.
- Commit `92cb5f6`: 12 files, +1020/-48. Production: new `plugin_playbooks/v2/dry.py` (296), new `plugin_playbooks/v2/skill.py` (122), `plugin_playbooks/v2/loop.py` (+128/-), `plugin_playbooks/v2/shim.py` (27), `plugin_playbooks/agent_tools.py` (73), `plugin_playbooks/__init__.py` (39), `plugin_playbooks/luna-plugin.toml` (4), `pyproject.toml` (2), `docs/v2.md` (32). Tests: new `tests/test_v2_dry_run.py` (243), new `tests/test_v2_skill.py` (91), `tests/test_v2_format_tools.py` (11). Secrets grep over the diff: only `secrets.randbelow`.
- Commands (plugin venv, `.venv/bin/python -m pytest … -q -p no:cacheprovider`, no `uv sync`):
  - `tests/test_v2_dry_run.py tests/test_v2_skill.py` → 17 passed (10 + 7; real jail present, `-rs` shows no SKIPPED/XFAIL).
  - Guard set `tests/test_v2_format_tools.py tests/test_manifest_drift.py tests/test_v2_contract_doc.py tests/test_no_spec_feature.py tests/test_dry_stub_diagnostic.py tests/test_plan026_navigable_dry_stubs.py` → 72 passed (`test_version_stamps_agree`: 0.49.0 at `pyproject.toml:3`, `plugin_playbooks/luna-plugin.toml:2`, `plugin_playbooks/__init__.py:649`; tool count 25 unchanged).
  - Full suite `tests -q -rfEsx` → 7 failed, 571 passed in 39.90 s (executor 40.22 s); 578 collected = plugin/04's 561 (554 + 7) + 17 new.
  - Verifier ran the two red files at `HEAD~1` in a temporary worktree (removed) and diffed against HEAD: failure lines identical except addresses/timings — none flipped.
  - Executor note: in one early isolated run 2 `tests/test_delegation.py` tests hit `TimeoutError` under a parallel jail load; passed 17/17 alone and in the full suite — not attributable to this change.
- Independent verifier re-ran every command above at `92cb5f6` with identical totals; 0 fix rounds.

## Results
Exit tests `tests/test_v2_dry_run.py` (real jail, real shim):
- `test_drystub_semantics` → PASS (`bool` True, iterates once, `str()` `<dry:a#1.x.y>`, arithmetic → `DryStubError`; the playbook catches `Exception` and asserts `type(e).__name__ == "DryStubError"` — see Deviations 9).
- `test_unstubbed_loop_runs_once_and_reports_unreached` → PASS (`steps_ran` keys `{fetch#1, send#1}`, `unreached_call_sites == [{"id": "never", "kind": "tool", "line": 6}]`, `status == "simulated"`).
- `test_drystub_error_names_effect_path_and_key` → PASS (`error` contains `fetch#1`, `total`, `stubs={"fetch#1"`; `"Traceback" not in json.dumps(result)`).
- `test_stubs_per_occurrence` → PASS (`page#2` `stubbed True`, `page#1`/`page#3` placeholders).
- `test_status_simulated_nothing_exercised` → PASS (`unreached_call_sites` lists `x`/tool and `y`/llm).
- `test_dry_run_writes_no_run_rows` → PASS (`PlaybookRun` 0, `PlaybookStepRun` 0, every journal row `dry is True`, entry 0 `mode == "dry"`).
- `test_per_kind_answers` → PASS (`approve` → `{approved: True, request_id: "dry:a#1", reason: None, decided_by: None, dry: True}`; `now`/`random` identical across two runs; site ids `a#1`/`t#1`/`r#1`/`log#1`).
- `test_dry_intake_coerces_and_fails_loud[integer]`, `[number]` → PASS (`"4"` → `4` through `playbook_dry_run`, journal entry 0 shows the coerced value; `"abc"` → `status == "rejected"`, `input == "count"`, no `journal` key).
- `test_tool_dispatch_by_format` → PASS (python → v2 shape; pblang → v1 trace with `trace`/`references`, no `steps_ran`).

Exit tests `tests/test_v2_skill.py`:
- `test_size_bound` → PASS (5116 bytes ≤ 6144).
- `test_examples_compile` (checker only, no jail) → PASS; `test_examples_compile_and_dry_run` → PASS (2 ```python blocks == the 2 blocks of `docs/v2.md`; checker ok; both dry-run to `status == "simulated"` with `stubs={}`, `error_type is None`).
- `test_honesty_rules_verbatim` → PASS (all four sentences in `V2_SKILL_BODY` and `_AUTHORING_SKILL_BODY` after whitespace normalisation).
- `test_loop_v2_order_and_no_validate` → PASS.
- `test_registered_next_to_v1` → PASS (`manifest.skills` names == `{playbook-authoring, playbook-delegation, playbook-authoring-v2}`; 12 tools, no `playbook_language_reference`, no `playbook_spec_*`).
- `test_publish_rule_in_tool_description` → PASS (`PUBLISH_RULE` is a substring of the `playbook_publish` description via `build_tools(None, _Bus(), _StubRunner())`).

Existing suite gate: 7 red, exactly plugin/04's list — `tests/test_repro_fixplaybooks_lifecycle.py` ×3 (`test_publish_success_carries_verified_readback`, `test_approved_then_regated_same_payload_trips_loop_guard`, `test_manifest_set_does_not_flip_live`), `tests/test_repro_fixplaybooks_runtime.py` ×4 (`test_interrupted_run_survives_restart_instead_of_failing`, `test_wait_for_approval_actually_gates`, `test_wait_for_event_actually_waits`, `test_tool_step_timeout_is_enforced`). None flipped. `_StubRunner` (lifecycle `:38-44`, manifest_drift) still imports and builds tools — the python dispatch needs no runner attribute at construction. `tests/test_v2_format_tools.py`: the "not available yet" python dry-run pin rewritten to the v2 shape (assertions strengthened, none removed); no skip/xfail added, no test deleted. No rider (`LANGUAGE_CHEATSHEET`/`LANGUAGE_MINIREF`/`references`) attaches on the python dry path.

Bench (Steps 9-11): NOT run in this part. Step 9 (side-load / `run.json.plugin_versions["plugin-playbooks"] == "0.49.0"`), Step 10 (dojop/01 `authoring-stateful-queue-v2`, `editing-cross-cutting-v2`, `authoring-within-budget`, `--trials 5`) and Step 11 (STOP RULE) are handed to the bench agent; the dojop/01 `summary.md` verdict block is to be appended below this line when it exists. Cross-repo checks (dojoP twin task existence, luna) not performed. Outcome (appended below by the bench agent): verdict stop — plugin/06 is not unblocked.

### dojop/01 bench verdict (copied verbatim from dojoP `plans/0002-fix-playbooks-bench/phases/01-v2-twin-tasks-and-go-no-go/execution_summary.md`)

Run `results/0059-run` (hermetic, `--trials 5 --tags fix-playbooks --ids playbooks.authoring-stateful-queue-v2,playbooks.editing-cross-cutting-v2,playbooks.authoring-within-budget`; 2026-09-07T23:06:21Z → 23:17:26Z, 665 s). Build under test: plugin-playbooks working tree at `6f49a97` (v2-runtime), loaded from the `LUNA_PLUGIN_SET_DIR` image-set `~/.luna/bench-set-0002-01` — `run.json plugin_versions["plugin-playbooks"] = "0.49.0"`, `plugin_upgrades = {"ok": true, "upgraded": [], "errors": [], "skipped": []}`, serve.log `plugins.winner_load_failed` 0 hits (the `plugins.source_winner` INFO line is dropped by alembic's root-WARN `fileConfig`; `resolve_plugin_winners` reproduced server-free: winner image-set 0.49.0, loser managed 0.46.0). luna `fix-playbooks` at `ecff9cd`; judge claude-haiku-4-5-20251001 (advisory).

| task | n | c | pass^5 | pass^2 | class | tool_calls_mean | failing_checks | 0055 (n, c, pass^2) |
|---|---|---|---|---|---|---|---|---|
| playbooks.authoring-stateful-queue-v2 | 5 | 5 | 1.0 | 1.0 | reliable | 1.0 | — | 2, 1, 0.0 |
| playbooks.editing-cross-cutting-v2 | 5 | 0 | 0.0 | 0.0 | broken | 2.1 | `playbook_edit|playbook_agent args match /ctx\.gather\(/` (turn 2, 5/5) | 2, 2, 1.0 |
| playbooks.authoring-within-budget | 5 | 5 | 1.0 | 1.0 | reliable | 1.0 | — | not in 0055 |

Rule applied: k=5 (the run's own `pass_hat_k`), pass^2 restated — same outcome under both. `playbook_validate` calls: 0 in all 15 trials; edit-turn tool calls 2, 2, 2, 2, 5; no hard timeouts. editing-v2 failure: all five edit bodies implement "4 at a time" as sequential slicing (`for i in range(0, len(entries), 4): batch = entries[i:i + 4]; for entry in batch:`), no `ctx.gather(` — the turn-1 seed is a pure-compute loop with nothing to gather (task-design finding for luna-fixer's re-plan; task not edited).

VERDICT: stop — authoring-stateful-queue-v2 beat 0055 (5/5, pass^5 1.0 and pass^2 1.0 vs 0055's n=2 c=1 pass^2 0.0) and authoring-within-budget is reliable (5/5), but editing-cross-cutting-v2 did not beat 0055 (0/5, pass^5 0.0 and pass^2 0.0 vs 0055's n=2 c=2 pass^2 1.0; sole failing check `ctx\.gather\(` on turn 2 in all five trials) — no P2 work (plugin/06, luna/02, M2) until luna-fixer re-plans in test-report.md.

dojoP commit: `512b1a53ce63933c4e6a595ce800fd4265e62d4b` (origin/main, subject `0002/01: v2 twin tasks and go/no-go`, verdict stop).

## Deviations from this plan
1. Comparison on a placeholder does not raise: `<`, `<=`, `>`, `>=` return `True` and `in` returns `True` (`v2/dry.py:181-199`). Required by this phase's own exit test — doc example 1 (`r["score"] > 3`) must dry-run to `simulated` with `stubs={}`. Arithmetic, `int()`, `float()`, index still raise `DryStubError`. §Scope item 1 and Risks 6 said "arithmetic/comparison"; the exit test won. Documented in `docs/v2.md` §10.
2. All `DryStub` placeholders compare equal to each other and to no real value (`__eq__`/`__hash__`, `v2/dry.py:204-208`): without it doc example 2's `while queue:` / `link not in seen` grew to `MAX_EFFECTS`. Documented in §10.
3. Dry result carries `result`, `error`, `error_type` beyond the plan's key list, and `format` from the tool; the intake rejection shape also carries `dry_run`, `format`, `tested_version`, `is_candidate`.
4. Line drift: `playbook_publish` ToolDef at `agent_tools.py:2987` (plan cited :2535), `_dry_run` :1927 (plan :1926), `_resolve_target` :1886, `playbook_dry_run` ToolDef :1991 — located by symbol.
5. `_perform_dry` (`v2/loop.py:547`) is synchronous; the subtask output schema is derived from `returns=` (list → key set), llm/agent from `output=` (`_declared_schema`, `v2/dry.py:253`).
6. The skill's ctx table is condensed from `docs/v2.md` §2 (not a verbatim copy) to fit 6144 bytes with both examples intact (Risks 2 said examples shrink first; the table shrank instead, examples stayed byte-for-byte). Honesty rules and `PUBLISH_RULE` are verbatim; no test compares the table.
7. `luna-plugin.toml` `playbook_dry_run` description reworded for the format-aware behaviour (drift test compares count/policy only).
8. `hash_seed` is 0 in dry mode (`v2/loop.py:285`); entry 0 `mode` is `"dry"` (live writes `"real"`).
9. `test_drystub_semantics`: the jail exposes no bare `DryStubError` name to playbook code (`__builtins__` only), so the playbook catches `Exception` and the test asserts `type(e).__name__`.
10. `test_per_kind_answers` asserts site ids `a#1`/`t#1`/`r#1`/`log#1` (assignment-target rule), not `approve#1`.
11. A second, jail-free `test_examples_compile` was added next to `test_examples_compile_and_dry_run` so the compile half runs where the jail is absent.
12. Steps 9-12's bench half deferred (above); this summary is Step 12's document half.

## Learned
- Wiring at `92cb5f6`: `SegmentLoop.__init__(session_factory, tools, events, ctx, journal, *, segment_timeout=60, max_effects=200, agent=None, start_run=None, mode="live", stubs=None)` (`v2/loop.py:181-188`, `ValueError` on any other mode); `SegmentLoop.dry` property :214; `SegmentLoop.dry_run(playbook, inputs=None, stubs=None, *, version=None) -> dict` :218 builds a SIBLING `SegmentLoop(mode="dry", stubs=)` per call on a throwaway `MemoryJournalStore(keep_completed=True)` (:234) with a transient `_DryRun(id=uuid4(), playbook_version=…)` (:114) — the live loop's journal store is never touched by a dry run; `drive()` :263 (unchanged for live; `hash_seed = 0 if self.dry` :285, entry 0 `mode="dry"|"real"` :288, `self._dry_rng[run_id] = Random(0)`); `_perform_dry` :547. `PlaybookRunner._v2` is the live loop; the tool calls `runner._v2.dry_run(...)`.
- `v2/dry.py`: `DRY_BANNER` :22; `DRY_CLASSES_SOURCE` :31 (stdlib-only text of `DryStubError` :32, `_pb_dry_sample` :46, `_pb_dry_wrap` :78, `_pb_DryDict` :91, `DryStub` :108, `_pb_dry_json_default` :229) spliced into `SHIM_SOURCE` at the marker `# @@DRY_CLASSES@@` (`v2/shim.py:38` `_DRY_MARK`, :139); host side `resolve_stub(stubs, effect_id, occurrence) -> (found, value, matched_key)` :242 (order `"<id>#<n>"` → `"<id>"`), `_declared_schema` :253, `dry_answer(kind, effect_id, occurrence, stubs, *, args=None, rng=None) -> (result, extra)` :266 where `extra = {stubbed, effect, schema, stub_key}`; `STUBBED_KINDS` = tool/llm/agent/subtask; `DRY_NOW = "2000-01-01T00:00:00+00:00"`. `wait_event` has no dry answer yet (`ValueError` "has no dry answer") — plugin/07 adds it in `dry_answer` and `_perform_dry`.
- Dry journal rows: `{…, dry: true, stubbed, stub_key, schema, effect: "<id>#<n>"}`; `_pb_decode_result` (`v2/shim.py:336`) rebuilds the placeholder or wraps the stub; `_pb_json_norm` :236 turns placeholders into `"<dry:<id>#<n>.<path>>"` strings in args and the return value. Bare placeholder `str()` is `<dry:<id>#<n>>` (path empty).
- Result dict from `dry_run`: `{status: simulated|simulated_nothing_exercised, dry_run: True, banner, steps_ran: {"<id>#<n>": {kind, args, result, stubbed}}, unreached_call_sites: [{id, kind, line}], journal, result, error, error_type}`; `playbook_dry_run` adds `format`, `tested_version`, `is_candidate`. `unreached_call_sites` = checker `summary["call_sites"]` minus ids reached (by id, not occurrence). Intake rejection through the tool: `{status: "rejected", error, input, expected, dry_run: True, format, tested_version, is_candidate}` for both formats (the pblang path's only behaviour change).
- `agent_tools.py` at `92cb5f6`: `_resolve_target` :1886, `_dry_run` :1927 (format branch :1960, `runner._v2.dry_run` :1969, `runner.dry_run` :1973, `except InputTypeError` :1974), `playbook_dry_run` ToolDef :1991, `playbook_publish` ToolDef :2987 (description ends with `PUBLISH_RULE`, imported at :48 from `v2/skill.py`). `__init__.py`: stamp :649, `skills=[` :664, v1 `SkillDef` :665, v2 `SkillDef` :697 (`name="playbook-authoring-v2"`), delegation :729, `_register_tool` :959, `_AUTHORING_SKILL_BODY` :213, `_DELEGATION_SKILL_BODY` :588. `v2/skill.py`: `V2_SKILL_MAX_BYTES` :13, `PUBLISH_RULE` :15, `V2_SKILL_BODY` :20 (5116 bytes). `docs/v2.md` `## 10. Dry run` :448-494 (12 sections, unchanged count).
- The v2 skill body contains no sentence about restarts (`grep -n "restart\|survive" plugin_playbooks/v2/skill.py` empty) — plugin/06's interim-text grep has nothing to remove there.
- Suite baseline for plugin/06: 578 collected = 571 green + 7 red at `92cb5f6`; stamps 0.49.0 (so plugin/06 stamps 0.50.0); tools 25; tables 10; skills 3.
- Version choice: separate minor bump per phase (0.48.0 → 0.49.0), not batched (Risks 10).

## Revised
- This file's phase `PLAN.md`: `Status: done`.
- `phases/06-durable-journal-and-resume/PLAN.md`: Depends line (plugin/05 landed at `92cb5f6`, STOP RULE verdict pending); Scope `luna-plugin.toml` bullet (stamp `__init__.py:649`; 0.49.0 → 0.50.0); Scope `journal.py` bullet (dry runs build a sibling loop with their own `MemoryJournalStore(keep_completed=True)` in `SegmentLoop.dry_run`); Scope interim-text bullet (v2 skill carries no restart sentence); Step 3 (the dry wiring as landed); Cross-repo plugin/05 line; Risks 6 (0.50.0 confirmed).
- `phases/07-parked-runs/PLAN.md`: Depends line (`dry_answer` signature and `_perform_dry` location, `wait_event` currently `ValueError` in `dry_answer`); Scope `loop.py` bullet (dry `approve` answer as landed; where the dry `wait_event` answer goes); Scope version bullet (0.49.0 at phase 05 confirmed); Step 10 (the two functions to edit); exit test 18 (`str()` form of a bare placeholder confirmed `<dry:mail#1>`, the in-jail classes live in `DRY_CLASSES_SOURCE`).
- `phases/08-lifecycle-integration-and-parity/PLAN.md`: Depends line (`resolve_stub` returns `(found, value, matched_key)`; `dry_answer` `extra` fields; landed commit); Scope `stubs_from_run` bullet (`_dry_run` :1927, ToolDef :1991 at `92cb5f6`; result keys as landed incl. `result`/`error`/`error_type`/`format`); Scope "Manifest and version" (stamp `__init__.py:649`, 0.49.0 at phase 05); Step 1 (plugin/05's summary says VERDICT pending until the bench appends its block); Cross-repo plugin/05 line.
- Repo `PLAN.md`: no correction requested by this phase file; none made. luna-fixer M1 summary and dojoP are the bench agent's (Steps 9-11).
