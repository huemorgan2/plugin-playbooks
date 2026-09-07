# 032 — Phase 05: Dry run on the same loop, the v2 skill, and the bench go/no-go

Status: pending

Master: §2 Dry run; §2 Prompt surface; §3 P1 (exit tests, "Bench go/no-go", STOP RULE); §3 Success criteria; master phase M1

Repo / branch: plugin-playbooks v2-runtime (HEAD 8c31a60 at writing)

Depends on: plugin/03 (effects on the loop), plugin/04 (`format` column, loud intake, format-aware tool descriptions), plugin/00 (`stubs=` exposed on `playbook_dry_run`, specs tools gone); dojop/01 runs the bench this phase records.

Unblocks: plugin/06 (only on `VERDICT: go`), plugin/08 (`stubs_from_run=` reuses this phase's per-occurrence stub keys and `unreached_call_sites`).

## Goal

A python-format playbook dry-runs on the same segment loop that runs it for real, with every effect answered from `stubs` or a once-iterating `DryStub`, no run rows written, `unreached_call_sites` and `steps_ran` reported per occurrence, and `DryStubError` instead of tracebacks; the agent gets one v2 skill (≤ ~6 KB, size-tested, two compiled-and-dry-run examples, THE LOOP honesty rules verbatim) registered next to the v1 skill; then dojop/01's twin-task bench decides go/no-go against results/0055-run, and the STOP RULE gates plugin/06.

## Scope — changes

Anchors are at HEAD 8c31a60; plugin/01–04 will move lines in `agent_tools.py` and `__init__.py` — re-verify symbols before editing.

1. New `plugin_playbooks/v2/dry.py` (module layout per repo `PLAN.md` "Conventions"):
   - `class DryStub`: placeholder for an unstubbed effect; `_dry = True`, `_effect` (`"<id>#<n>"`), `_path`; `__bool__` → `True`; `__iter__` yields exactly one child `DryStub` (path `[0]`); key/attribute access returns a nested `DryStub` when the effect has no schema, or the schema's typed sample when it has one (`ctx.llm(schema=)`, `ctx.agent(schema=)`, `ctx.subtask` → the child's declared inputs/outputs); a key outside a declared schema raises `DryStubError`; `__str__` → `<dry:<id>#<n>.<path>>` (same form as v1 `_DryStub.__str__`, runner.py:99-100) so a placeholder can be passed into the next effect's args and journaled; arithmetic/comparison on a placeholder raises `DryStubError` (master §2 Dry run: no bare `TypeError`).
   - `class DryStubError(Exception)`: message `dry run: effect <id>#<n> has no stubbed value at <path>; pass stubs={"<id>#<n>": {...<path>...}} (or "<id>" for every occurrence)`. Raised inside the jail, it travels through plugin/02's `outputs/error.json` contract as `error_type: "DryStubError"` and is surfaced verbatim in the dry-run result — never a traceback.
   - Provided stubs are wrapped the same way: a `stubs` dict/list value whose key the code reads but the stub lacks raises `DryStubError` naming the stubs key.
   - `resolve_stub(stubs, effect_id, occurrence)`: lookup order `"<id>#<n>"` → `"<id>"` (call-site name = the `_id=` kwarg, else the checker's derived call-site name from plugin/01) → placeholder.
   - `dry_answer(kind, effect, stubs, occurrence)` per kind: tool/llm/agent/subtask → stub or placeholder; approve → `{"approved": True, "dry": True}`; now → fixed timestamp `2000-01-01T00:00:00+00:00`; random → `random.Random(0)` sequence per run (both journaled like live values); wait_event → checker-rejected until plugin/07 (see Not in this phase).
   - `dry.py` imports stdlib only, because the shim must rebuild `DryStub` inside the jail from the journal entry (`{"dry": true, "stub": <json|null>, "schema": <json|null>, "effect": "<id>#<n>"}`); it is copied into the run dir next to `plugin_playbooks/v2/shim.py` (plugin/02's "in-jail file copied into each run dir").
2. `plugin_playbooks/v2/loop.py`: add `mode: "live" | "dry"` to the segment loop entry point plugin/02 defined. In dry mode: (a) effect execution is replaced by `dry_answer`; (b) every journal entry carries `dry: true`; (c) nothing is written to `playbook_runs` (`PlaybookRun`, models.py:134-178) or `playbook_step_runs` (`PlaybookStepRun`, models.py:180-200), no `playbook.run.*` events are emitted, and the in-memory `JournalStore` is discarded with the result; (d) the intake step is the same loud coercion plugin/04 wired for chat/trigger (`_coerce_inputs`, runner.py:192-230, whose silent branch at :228-229 plugin/04 makes loud) — a bad input fails before segment 1. Result dict: `{"status": "simulated" | "simulated_nothing_exercised", "dry_run": True, "banner": <v1 banner text, runner.py:593-596>, "steps_ran": {"<id>#<n>": {"kind", "args", "result", "stubbed": bool}}, "unreached_call_sites": [{"id", "kind", "line"}], "journal": [...], "tested_version", "is_candidate"}`. `unreached_call_sites` = plugin/01 checker's static call-site list minus ids present in the journal; `simulated_nothing_exercised` when the journal has zero effects (early return, exception before the first effect).
3. `plugin_playbooks/agent_tools.py::_dry_run` (:1572-1633): keep the JSON parse (:1573-1576) and the version resolution (:1585-1625); after `target` is resolved branch on the target's `format` (plugin/04 column): `"python"` → the v2 loop in dry mode with `stubs`; `"pblang"` → `runner.dry_run(target, inputs=..., stubs=...)` exactly as phase 00 left it (:1627). Keep `tested_version`/`is_candidate` (:1629-1632). Rewrite the `playbook_dry_run` description (:1639-1647) format-aware: for python, name `stubs` keys `"<id>#<n>"`, `unreached_call_sites`, and "outputs are SIMULATED — never report them as real results". `stubs` is already in the ToolDef schema from phase 00 (specs-removal.md, phase 00); nothing new in `luna-plugin.toml` `[[tools]]` (tool count unchanged).
4. New `plugin_playbooks/v2/skill.py`: `V2_SKILL_BODY` (str) and `V2_SKILL_MAX_BYTES = 6144`. Sections, in order: (1) the ctx contract table copied from `docs/v2.md` (plugin/01); (2) THE LOOP v2 — `playbook_propose`/`playbook_edit` (compiles + checks in one call; a green write carries `"validated": true` — do not call `playbook_validate` after it) → `playbook_dry_run` → `playbook_run_candidate` → `playbook_publish`; (3) the honesty rules from the v1 skill, byte-for-byte after unwrapping the source's line breaks: "Never run blind:" (`__init__.py:208`), "Outputs are SIMULATED: NEVER report a dry-run value as a real result." (:220-221), "never re-run a 'running' playbook or invent results." (:226), "NEVER report an edit as done after `candidate_saved` — the old version runs until publish succeeds." (:377-378); (4) candidate-vs-live in five lines; (5) the owner-intent-to-publish rule (one sentence: publish only when the owner asked for this change to go live; a green candidate run is evidence, not permission); (6) what runs where: playbook body in the jail, effects on the host, approve/wait parks the run; (7) TWO worked examples in ```` ```python ```` fences (a fetch → `for` → `ctx.gather` fan-out with `_id=`; an `ctx.llm(schema=)` → `if` → `ctx.approve` → `raise` on rejection), each compilable by the checker and green under `mode="dry"` with no stubs.
5. `plugin_playbooks/__init__.py`: a third `SkillDef` in `manifest.skills` (:639-693) — `name="playbook-authoring-v2"`, `body=V2_SKILL_BODY`, `tools=[playbook_propose, playbook_edit, playbook_manifest_set, playbook_publish, playbook_rollback, playbook_run_candidate, playbook_get_definition, playbook_validate, playbook_dry_run, playbook_set_autonomy, playbook_list_available_triggers, playbook_preflight]` (v1 list :657-671 minus the five `playbook_spec_*` tools phase 00 removes and minus `playbook_language_reference`, which stays pblang-only per repo `PLAN.md` risk 8). `AUTHORING_TOOLS` (:880-902) is a superset, so the phase-3 rule (:892) and `_register_tool`'s `skill_gated=True` (:913-941) need no change. The v1 description (:642-650) gains "for pblang playbooks (`playbook(` source)"; the v2 description says "for python playbooks (`async def run(ctx, inputs)`), the default for new playbooks". `_DELEGATION_SKILL_BODY` untouched.
6. `playbook_publish` ToolDef description (agent_tools.py:2535-2547): append the owner-intent-to-publish sentence, identical to the skill's (a test compares them).
7. `docs/v2.md` (plugin/01): add the dry-run result keys and the `stubs` key shape above; the skill's contract table is copied from this file, not the other way round.
8. Versions: an in-code manifest skills change bumps the minor (repo `PLAN.md` "Conventions"); the three stamps `pyproject.toml:3`, `luna-plugin.toml:2`, `__init__.py:624` move together to plugin/04's version + 0.1.0 (record the number in the summary). No `[[tools]]`, `db_tables`, UI, or DB change.
9. New tests `tests/test_v2_dry_run.py`, `tests/test_v2_skill.py` (Exit tests).

## Not in this phase

- `stubs_from_run=<run_id>` (master §2 Dry run, P3) — plugin/08; this phase fixes the `"<id>#<n>"` key shape it will fill.
- `ctx.wait_event` dry answer (stub payload or `EventTimeout`) — plugin/07; until then the checker rejects `wait_event` (repo `PLAN.md` phase index) so the dry path never sees it.
- The provenance envelope that puts `status: simulated` first in key order — plugin/09; here `status` is a plain key.
- The v1 dry run (`runner.dry_run` :557-562, `_DryStub` :75-103, loop-over-stub `items = []` :1196-1199) is untouched; pblang playbooks keep v1 semantics.
- Running the bench, authoring `*-v2.yaml` tasks, the `playbook_create` step op, hermetic tooling — dojop/01. This phase supplies the build, the version proof, and records the verdict.
- The delegation v2 prompt variant and the canvas journal overlay — later phases per the repo index.

## Steps

1. Read plugin/02–04 `execution_summary.md`; pin the loop entry signature, the checker's call-site list shape, plugin/04's `format` accessor and loud-intake helper. Done when the symbol names in §Scope items 2–3 are corrected in this file (edit before coding, note it under Deviations if anything moved).
2. Write `v2/dry.py`. Done when `tests/test_v2_dry_run.py::test_drystub_semantics` passes: `bool(DryStub(...)) is True`, `len(list(DryStub(...))) == 1`, nested access returns a `DryStub` with the extended path, `str()` is `<dry:fetch#1.items[0].email>`, `DryStub(...) + 1` raises `DryStubError`.
3. Add `mode="dry"` to `v2/loop.py` with the journal flag, no-row rule and intake coercion. Done when a two-effect example dry-runs green with `stubs={}` and a `select(PlaybookRun)` / `select(PlaybookStepRun)` count on the test engine is 0 before and after.
4. Add `steps_ran`, `unreached_call_sites` and the two statuses. Done when a playbook with an `if` that skips a call site lists that site, and `return` before the first effect yields `simulated_nothing_exercised`.
5. Wire `_dry_run` dispatch and the format-aware description. Done when the pblang path is byte-identical in behaviour (existing `tests/test_plan026_navigable_dry_stubs.py`, `tests/test_dry_stub_diagnostic.py`, `tests/test_plan026*` green) and the python path returns the §Scope result shape through `build_tools`.
6. Write `v2/skill.py`, register the SkillDef, extend the `playbook_publish` description. Done when `tests/test_v2_skill.py` is green and `tests/test_manifest_drift.py` is green (skills are not in `luna-plugin.toml`; the tool set did not change).
7. Bump the three version stamps. Done when `tests/test_manifest_drift.py::test_version_stamps_agree` (:69-78) passes.
8. Run the suite (`uv run pytest tests` in the plugin repo, read-only besides temp SQLite). Done when the count is plugin/04's green count + the new tests, and exactly the same 7 red repro tests are red (none of them belongs to this phase — repo `PLAN.md` "7 red repro tests").
9. Build proof for the bench: side-load the branch on a `vaselin-*` agent (branch build, no publish) or hand dojop/01 the `LUNA_PLUGIN_SET_DIR` copy of `plugin_playbooks/`; run one v1 playbook there (M1 step 3). Done when the served manifest version equals this phase's stamp (the marketplace 0.46.0 copy must lose, dojop/01 "version proof").
10. dojop/01 runs `authoring-stateful-queue-v2`, `editing-cross-cutting-v2`, `authoring-within-budget` at `--trials 5` (`run.py:421-428`; results dir from `_next_run_dir`, `run.py:61-67` — 0059-run or later, 0058-run is the latest at writing). Done when its `summary.md` verdict block (`VERDICT: go | stop`, pass^k table, versions, run dir) is copied verbatim into this folder's `execution_summary.md`.
11. Apply the STOP RULE (master §3 P1): `go` → mark plugin/06 unblocked; `stop` → do not start plugin/06, write the diagnosis into luna-fixer `plans/2026-09-06-fix-playbooks/test-report.md`, re-plan. Done when the summary states the decision and the M1 summary in luna-fixer carries the same verdict.
12. Write `execution_summary.md`; revise `06-durable-journal-and-resume/PLAN.md` and `08-lifecycle-integration-and-parity/PLAN.md` with the final stub key shape, result keys and loop signature. Done when both files cite this summary.

## Exit tests

`tests/test_v2_dry_run.py` (fixtures: `plugin_playbooks.testing.build_test_runner` / `make_fake_tool` / `make_playbook`, or an in-memory `create_async_engine("sqlite+aiosqlite://")` + `Base.metadata.create_all` as the existing suite does):
- `test_unstubbed_loop_runs_once_and_reports_unreached`: `rows = await ctx.tool("fetch")` then `for r in rows: await ctx.tool("send", to=r["email"], _id="send")` followed by an `if False`-guarded `ctx.tool("never")`; assert `steps_ran` has `fetch#1` and `send#1` only, `unreached_call_sites == [{"id": "never", ...}]`, `status == "simulated"`.
- `test_drystub_error_names_effect_path_and_key`: stub `{"fetch#1": {"items": []}}`, code reads `rows["total"]`; assert the result's `error` contains `fetch#1`, `total`, and `stubs={"fetch#1"`; assert `"Traceback" not in json.dumps(result)`.
- `test_stubs_per_occurrence`: a loop calling `ctx.tool("page", _id="page")` three times with `stubs={"page#2": {"n": 2}}`; assert `steps_ran["page#2"]["stubbed"] is True` and `#1`/`#3` are placeholders.
- `test_status_simulated_nothing_exercised`: `return` before any effect → `status == "simulated_nothing_exercised"`, `unreached_call_sites` lists every site.
- `test_dry_run_writes_no_run_rows`: `count(*)` on `playbook_runs` and `playbook_step_runs` is 0 after the dry run, and every journal entry has `dry is True`.
- `test_per_kind_answers`: approve → `{"approved": True, "dry": True}`; `now`/`random` values are identical across two dry runs.
- `test_dry_intake_coerces_and_fails_loud`: `inputs_schema` declares `count: integer`; `inputs={"count": "4"}` reaches `run()` as `4` (journal entry 0 shows `4`); `"abc"` returns an error naming `count`, and the journal is empty.
- `test_tool_dispatch_by_format`: through `build_tools(...)["playbook_dry_run"]`, a `format="python"` playbook returns the v2 shape; a pblang playbook returns v1's `"dry_run": True` trace (`runner.dry_run` :591-609) unchanged.

`tests/test_v2_skill.py`:
- `test_size_bound`: `len(V2_SKILL_BODY.encode("utf-8")) <= V2_SKILL_MAX_BYTES` (6144).
- `test_examples_compile_and_dry_run`: `re.findall(r"```python\n(.*?)```", V2_SKILL_BODY, re.S)` yields exactly 2 blocks (mirrors `tests/test_pblang.py::test_skill_examples_compile` :468-478); each passes the plugin/01 checker with zero errors and dry-runs to `status == "simulated"` with `stubs={}` and no `DryStubError`.
- `test_honesty_rules_verbatim`: the four sentences in §Scope item 4(3) are substrings of `V2_SKILL_BODY` and of `_AUTHORING_SKILL_BODY` after `re.sub(r"\s+", " ", ...)` on both.
- `test_loop_v2_order_and_no_validate`: the strings `playbook_dry_run`, `playbook_run_candidate`, `playbook_publish` appear in that order and the body contains "do not call `playbook_validate`".
- `test_registered_next_to_v1`: `PlaybooksPlugin.manifest.skills` names == `{"playbook-authoring", "playbook-delegation", "playbook-authoring-v2"}`; every v2 skill tool is in `AUTHORING_TOOLS`; `playbook_language_reference` and `playbook_spec_*` are not in the v2 list.
- `test_publish_rule_in_tool_description`: the owner-intent sentence from the skill is a substring of the `playbook_publish` ToolDef description obtained via `build_tools(None, _Bus(), _StubRunner())` as `tests/test_manifest_drift.py::_code_tooldefs` (:40-47) does.

Existing suite gate: all tests green except the 7 red repro tests (`tests/test_repro_fixplaybooks_runtime.py` ×4, `tests/test_repro_fixplaybooks_lifecycle.py` ×3), which stay red — none flips in this phase. `tests/test_repro_fixplaybooks_lifecycle.py::_StubRunner.dry_run(self, playbook, inputs=None)` (:43-44) and `test_manifest_drift._StubRunner` must keep importing and building tools, so the python dispatch must not require new runner attributes at construction time.

Bench (recorded, not run here): dojop/01's `summary.md` verdict block in `execution_summary.md`; STOP RULE applied as in Steps 11.

## Cross-repo checks

- dojop/01 (`dojoP/plans/0002-fix-playbooks-bench/phases/01-…`): the twin tasks `authoring-stateful-queue-v2.yaml` and `editing-cross-cutting-v2.yaml` do not exist at f51915d (repo `PLAN.md` risk 7) — confirm they are committed before Step 10; `editing-cross-cutting-v2` seeds a `format: "python"` playbook through REST, which needs plugin/04's `format` on `PlaybookCreate`; the run is `--trials 5 --tags fix-playbooks` against the hermetic server (`tools/hermetic.sh`, boots `uv run luna serve --port` then `run.py run --base`) with `LUNA_PLUGIN_SET_DIR` pointing at a copy of this branch's `plugin_playbooks/`, or a `vaselin-*` agent carrying the branch; version proof = `run.json.plugin_versions["plugin-playbooks"]` equals this phase's stamp and `plugin_upgrades.upgraded` does not list plugin-playbooks (0055 shows `0.46.0`, the marketplace build).
- Baseline for the comparison: `results/0055-run` (base `http://127.0.0.1:8010`, 2 trials): `playbooks.authoring-stateful-queue` flaky 1/2 → pass^k 0.0; `playbooks.editing-cross-cutting` reliable 2/2 → pass^k 1.0 (`lib/stats.py::pass_hat_k` :18-25); `authoring-within-budget` was not in 0055's `filters.ids`.
- plugin/04: the `format` accessor used in `_dry_run`, the loud-intake helper, and the `format` echo in results are consumed here unchanged; the `LANGUAGE_CHEATSHEET`/`LANGUAGE_MINIREF` riders (agent_tools.py:104-115) must not attach on the python dry path.
- plugin/08: `stubs_from_run` fills `stubs` with `"<id>#<n>"` keys from a recorded journal; it relies on `unreached_call_sites` and `steps_ran` exactly as shaped here — any change after this phase is a plugin/08 revision.
- luna (fix-playbooks branch): no change. The core registers every `manifest.skills` entry (`luna/plugins/loader.py:825-831` → `luna/skills/registry.py::register` :14-23, name collision raises) and the agent loads a skill by name (`luna/agent/system_prompt.py::skills_block` :361-397; `luna/agent/read_tool.py::_resolve_skill` :293-299); there is no core format switch.

## Risks and open questions

1. Assumption — "chosen by format" has no core mechanism: the two authoring skills are both listed in the menu; steering is by their descriptions plus plugin/04's format-aware tool results. If the bench shows the agent loading the pblang skill for python work, the fix is description wording (this phase) or a core menu hint (a luna phase, owner decision).
2. Assumption — the size bound "≤ ~6 KB" is pinned as 6144 UTF-8 bytes. If the contract table plus two examples cannot fit, the examples shrink first; the honesty rules and the owner-intent rule never do.
3. Assumption — which v1 sentences are "THE LOOP honesty rules": the four listed in §Scope item 4(3); the fourth lives in v1's CANDIDATE → PUBLISH section (:377-378), not under THE LOOP (:207-228). The owner may want the OUTLINE FIRST rule (:209-213) carried too; it is pblang-shaped ("`id -> kind`") and is left out.
4. Assumption — the v2 skill keeps `playbook_validate` in its tool list (it stays format-aware from plugin/04 and is useful for code-only checks before `propose`) while the text says not to call it after a green write; `playbook_language_reference` is excluded because the v2 skill IS the reference (master §2 Prompt surface).
5. Assumption — `DryStub` must be rebuilt inside the jail, so `dry.py` is stdlib-only and shipped with the shim; if plugin/02 chose a different shipping mechanism (single-file shim), `DryStub` moves into `shim.py` and `dry.py` keeps only the host side (`resolve_stub`, `dry_answer`).
6. Assumption — placeholder without schema returns nested placeholders on access (so an unstubbed loop body runs to its end, which the exit test requires) and raises `DryStubError` only on arithmetic/comparison or on keys outside a declared schema; the master's "reading data a stub does not have" is read as covering declared schemas and provided stubs.
7. Assumption — the v2 loop object reaches `_dry_run` through the same `runner` argument of `build_tools(session_factory, events, runner, ctx)` (plugin/02 attaches it) so the two `_StubRunner` test doubles keep working; if plugin/02 added a separate kwarg, Step 1 records that and the tests pass it explicitly.
8. STOP RULE arithmetic: 0055's `editing-cross-cutting` is already pass^k 1.0 at k=2, which cannot be strictly exceeded. Reading applied here: `authoring-stateful-queue-v2` must be strictly above 0.0 and `editing-cross-cutting-v2` must be 1.0 at k=5 (5/5, a stricter estimator than 2/2); anything lower on either task is `VERDICT: stop`. Owner confirmation wanted before Step 11 runs on a borderline result.
9. Bench target: the master is silent; the hermetic server with the side-loaded branch is preferred (repeatable, no fleet touch); a `vaselin-*` agent is the alternative — never a customer machine, never a publish.
10. Version stamp: bumping the minor for a skills-only manifest change follows the repo convention; if plugin/04 already bumped in this same unpublished series the owner may prefer one batched bump — state which in the summary.
11. Fixed-seed choice for `now`/`random` in dry mode (`2000-01-01T00:00:00+00:00`, `Random(0)`) is not in the master; only "fixed seeds" is. Any value works as long as two dry runs agree.
12. Standing constraints: nothing is published, pushed to main, or promoted; plugin-playbooks and luna commits stay on their local branches; dojoP may commit and push to `novalystrix-org/dojoP`; only `vaselin-*` machines; secrets never committed or printed; `results/` transcripts must not contain tokens (dojop/01 checks before committing).

## Execution summary

Written to execution_summary.md in this folder after the phase runs, using this template:
- Ran: (commands, dates, HEAD before/after, results folder ids)
- Results: (every exit test with its outcome and the relevant output; anything red and why)
- Deviations from this plan: (what changed and why)
- Learned: (facts that change later phases)
- Revised: (which later phase files were edited because of this, and how)
