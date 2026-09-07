# 032 — Phase 00: Specs (Tests tab) removal
Status: pending
Master: §2 Specs removal (owner decision 2026-09-07), §3 P1 Step 0, §4 Rollout (branch rules), §6 Test summary (the 7 red repro tests); checklist `luna-fixer plans/2026-09-06-fix-playbooks/specs-removal.md` (authoritative hook list; its "Order of operations" 1-6 and "DB migration"); master phase M0 (`luna-fixer plans/2026-09-06-fix-playbooks/phases/M0-specs-removal/PLAN.md`)
Repo / branch: plugin-playbooks v2-runtime (code HEAD 8c31a60 at writing, plus the plan-only commit 5306f7f on top; origin/main 749f126, version 0.46.0)
Depends on: — (P0 plans are master prerequisites, not phase inputs; no phase in any repo precedes this one)
Unblocks: plugin/01 and through it plugin/02-12; luna/00 (runs luna 007.009 against this phase's commit) → luna/01; dojop/00 runs in parallel and shares the M0 gate

## Goal
Remove the stored playbook tests ("specs", the Tests tab) from the plugin in one commit on
`v2-runtime`, before any v2 code: tools, publish gate, routes, model, module, version copy,
load-time backfill, UI half, docs and prompt text. Keep dry run (which gains a tool-level `stubs`
parameter so the stub seam keeps a production caller), the `test_run` gate, probes/preflight,
versions, promotions and runs. Drop the two schema remnants on load, log and preserve what they
held, and leave a permanent guard test so the feature cannot creep back. Publish gates go from
`static_validation → specs → test_run → probes` to `static_validation → test_run → probes`.

## Scope — changes
Line numbers are at HEAD 8c31a60 and were re-verified for this file; they shift as deletions
land — edit each file top-down.

Package (`plugin_playbooks/`):
- `agent_tools.py`: imports :32 `PlaybookSpec`, :42 `specs_gate`, :47 `from .specs import …`,
  :49 `spec_source_version` — drop; `_gate_owner_line` :61-64 specs branch; `_status` hint
  :648-659 (names `playbook_spec_from_run`) → "Failed — every step that ran recorded its real
  output above; read the failing step's error before editing."; `_set_autonomy` :842-846
  signature (`require_specs` kwarg), guard :847-852, writes :875-876, :880, :887, description
  :904-915, schema :931-934; `_versions` :1131-1137/:1161/:1174-1182; `_version_read` :1196-1199
  `include_specs`, :1229-1244, :1268-1274, :1280; `_edit` :1869-1877
  `source_version=spec_source_version(...)`, :1882-1889 auto-run, :1910-1925 `result["specs"]`;
  `_manifest_set` :2005-2011 kwarg; `_request_publish_decision` :2057-2072 params, :2081,
  :2154-2163 "Specs" card entry; publish gate 2 :2314-2326, comments :2327/:2347/:2378,
  :2394-2405, :2414-2415, :2488-2490 `spec_gate_entry`, evidence keys :2502-2503, description
  :2535-2548; banner :2772; `_spec_target` :2774-2803 → renamed `_resolve_target`, moved above
  `_dry_run` :1572 (still inside `build_tools` :220); the five spec tools + ToolDefs :2805-3261
  — delete; `_preflight` :3272 call → `_resolve_target`, description :3312 → "the check a dry
  run can't do". `_dry_run` :1572 gains `stubs: str | dict = "{}"`, decoded like `inputs`
  :1573-1576, passed at :1627, declared in the ToolDef schema :1648-1660; its inline resolution
  :1585-1625 becomes one `_resolve_target` call (the only tests pinning its wording,
  `tests/test_candidate_flow.py:238/:273/:370` `"no candidate" in out["error"]`, match both).
- `publish.py`: `specs_gate` :310-366 — delete; `test_run_gate` comment :152-153 and sentence
  :157-158 — delete; `_RELAX` :104 and the hint :160-164 stay.
- `routes.py`: imports :30/:35/:36; `_shim_for` :440-460; `PublishSettingsPatch.require_specs`
  :558; `_trust_summaries` :568-609 spec half (probes :610-633 stay); `list_playbooks` fallback
  :660-663; `get_playbook` :711; PUT mint :797-802 kwarg; `patch_publish_settings` :886-906
  (guard :889 → `if body.require_run is None`, 400 text :890 names only `require_run`, writes
  :897-898, response key :904); `list_versions` :1065-1077/:1095/:1109; `_refresh_specs`
  :1168-1174 and its calls :1243-1244, :1287-1290, :1346-1348; promote docstring :1207; specs
  section :1381-1445 (GET `/playbooks/{name}/specs`, POST `.../specs/run`);
  `run_preflight_route` docstring :1481-1482 → "the Connections view"; `put_manifest`
  :1539-1541 (`old_live` local + kwarg).
- `models.py`: `Playbook.publish_require_specs` :71 (+ comment :69-70 → the single test_run
  gate); `class PlaybookSpec` :203-244 whole (blank :245-246 too).
- `versioning.py`: docstring :1-8, `Any` :12, import :17, `ensure_live_row` docstring :58-60,
  `copy_specs` :77-119, `mint_version` `source_version` param :131 + docstring :134-136 + call
  :168, `spec_source_version` :215-218. Lands in the same commit as the four
  `mint_version(source_version=)` callers (agent_tools :1871/:2010, routes :802/:1541) or
  `mint_version` raises TypeError.
- `specs.py` (363 lines) — delete the module.
- `runner.py` — rewordings only: :84-85, :142, :567 ('the playbook "test run"' → "the playbook
  simulation"), :837-838, :1821-1823, :1838, :1850, :1877. No code change:
  `dry_run(playbook, inputs=None, stubs=None)` :557-562 and the stub check :839-842 already do
  what the tool needs.
- `delegation.py`: :25; `_PHASE_BY_TOOL` :68/:76-79; `_GATED_TOOLS` :99;
  `_GATED_TOOL_OWNER_WORDS` :112; work-loop step "5. SPECS" :278-282 (renumber 6/7/8 → 5/6/7;
  the "Eleven sections" comment :196-199 and the section count stay —
  `tests/test_delegate_prompt.py:26-29` pins eleven); :324 `spec_run`; :328 `spec_delete`;
  sample report :364; checklist item :372 (renumber); `playbook_agent` description :804/:806;
  task example :820-821 → "test run must be green".
- `card.py` :183 `WAIT_WORDS.playbook_spec_delete`; `probes.py` :3-4; `reference.py` :115
  "(specs + a green test run gate it)" → "(a green test run gates it)" (:3, :110, :121 are not
  hooks — language "spec", Jinja "Tests:"; `validation.py:63` "specially" is not one either).
  Two verb-form hits of the guard's `\bTests\b` token, `__init__.py:220` and
  `agent_tools.py:1645` "Tests the CANDIDATE" (skill body, `playbook_dry_run` description) →
  "Exercises the CANDIDATE", so the token check needs no allowlist beyond `reference.py:110`.
- `__init__.py`: `_COLUMN_MIGRATIONS` :33 and :35-36, `_LEGACY_INDEXES` :45;
  `backfill_spec_versions` :87-124 + call :777-781; new `_drop_spec_remnants` after
  `_drop_legacy_indexes` :70-84, called after :728-731; skill body :374 "SPECS,", :380-394
  `### SPECS` section, :397/:400 preflight sentences, :409 recipe; :456 docstring; delegation
  skill :567/:568/:574/:597; SkillDef tools :664-668; `AUTHORING_TOOLS` :892-897;
  `_register_tool` comment :915 "all 18" → 13 (optional); version :624.

DB (plugin-owned; core never drops a table that leaves the manifest):
`async def _drop_spec_remnants(engine)` — `run_sync(inspect)`: if `has_table("playbook_specs")`:
log `SELECT COUNT(*) FROM playbook_specs`, then `DROP TABLE IF EXISTS playbook_specs`; if
`publish_require_specs` in `get_columns("playbooks")`: log `SELECT COUNT(*) FROM playbooks WHERE
publish_require_specs = FALSE` (the only non-default values), then `ALTER TABLE playbooks DROP
COLUMN publish_require_specs`. Same never-block-the-load try/except as :724-731. PG (asyncpg)
and SQLite ≥ 3.35 support both statements (`.venv` python: SQLite 3.50.4, SQLAlchemy 2.0.51);
the column is in no index or constraint (models.py:71), so SQLite's DROP COLUMN rule holds.

Manifest and versions: `luna-plugin.toml` :2 version, :8 `db_tables` minus `playbook_specs`,
:36 `tools = 25`, :37 `tables = 10`, :106/:112 descriptions, :141 dry_run block gains `stubs`,
:176-205 five `[[tools]]` blocks deleted from the header at :176 (deleting :177-205 leaves an
empty block and a KeyError at `tests/test_manifest_drift.py:52`). Version 0.47.0 in
`pyproject.toml:3`, `luna-plugin.toml:2`, `__init__.py:624`.

UI (`ui-src/src/playbooks/`): `types.ts` :94/:101-109/:127; `api.ts`
:3/:88-93/:125/:131-133/:154-173; `trust.ts` :1-3/:13-18/:50/:51-55; `TestsTab.tsx` →
`ConnectionsTab.tsx` (probes only, no `version` prop; CONNECTIONS :145-173, `checkNow` :74-86,
`ProbeRow` :228-255 stay); `VersionsTab.tsx` :4/:11/:22/:36-37/:40/:46/:242-243/:263/:396-402
(only these)/:559-560/:668-683; `PublishSettings.tsx` :7/:44-49; `PlaybookEditor.tsx`
:29-32/:60-63/:108-111/:230-243; `PlaybooksSection.tsx` :22/:260/:266. Tests:
`__tests__/TestsTab.test.tsx` deleted, replaced by `__tests__/ConnectionsTab.test.tsx`
(probes-only: `getProbes`/`runPreflight` mocks, `probes-headline`, `probes-check-now`);
`VersionsTab.test.tsx`, `trust.test.ts`, `PublishSettings.test.tsx` per the checklist's UI
section. Bundle: the rebuilt hashed pair under `plugin_playbooks/ui/assets/` committed,
`index-BqhDbTui.js` + `index-BgLNZxTK.css` deleted, `ui/index.html:7-8` updated by the build.

Plugin tests: `tests/test_specs.py` and `tests/test_versioned_specs.py` deleted; the three
dry-run tests (`test_specs.py:103/:131/:588`) rewritten in
`tests/test_plan026_navigable_dry_stubs.py` on `_bare_runner` :18-27 and `_pb` :30-35;
`test_publish_settings.py` and `test_tool_timeouts.py` inline their fixtures; edits in
`test_version_routes.py:198`, `test_plan022_truthful_evidence.py` (:1-5, :27, :279-346
deleted), `test_build_operate.py` (:350-351, :359-365, :374, :380),
`test_candidate_flow.py:255-257`, `test_manifest_drift.py:86-87`, `test_zero_yaml.py` (:85
required; :5, :22-24, :39-40 optional), `test_delegation.py` (:291-292, :327-336),
`test_delegate_prompt.py:80`, `test_card_route.py` (:87, :95), `test_dry_stub_diagnostic.py:3`.
New `tests/test_no_spec_feature.py` (guard items 1-6, below).

Docs: `README.md` :6-10, :38, :43-45, :62-105 cookbook (the `stubs` note :100-101 moves under a
new "Dry-run stubs" heading), :110-114, :119-120, :142; `vision/vision.md` :27-29, :33, :40,
:46, :61, item 5 :93-94 deleted, and a dated note under the removal bar :101-103: "2026-09-07:
the specs feature was removed on the owner's decision (luna-fixer
`plans/2026-09-06-fix-playbooks/PLAN.md` §2 Specs removal); the evidence bar is met after the
fact by the P1 go/no-go and the P4 keyhole gate, both measured without specs." (:55 stays).

## Not in this phase
- No v2 code, no `plugin_playbooks/v2/`, no `docs/v2.md` (phase 01 creates `docs/`; the
  directory does not exist at HEAD).
- `playbook_dry_run(stubs_from_run=)` and the `_status` hint sentence naming it — P3
  (plugin/08), only once the parameter exists.
- luna: the `tests/007.009-vibe-playbook/test_tools.py:29` comment naming `playbook_specs`
  rides the ship-time re-pin commit (luna/04); the stale vendored `luna/plugins/plugin_playbooks`
  is luna/01.
- dojoP: retiring the three spec tasks, `criteria.md:46`, `judge.py:111-116`,
  `candidate-then-publish.yaml:5`, the `_validate` rule — dojop/00. This phase records its verdict.
- The owner's PG drop rehearsal on a `vaselin-*` agent is an M0 owner touchpoint, not a repo
  change; this phase supplies the log lines it reads.
- No marketplace publish, no push, no version pin, no fleet image (master §4).
- Repro tests: none of the 7 red tests (`tests/test_repro_fixplaybooks_runtime.py` ×4,
  `_lifecycle.py` ×3) is expected to flip; they stay red.

## Steps
0. Preconditions. `git status --short` at HEAD 5306f7f (the plan-only commit on top of code HEAD
   8c31a60; `plans/032-v2-runtime/` is already committed) shows only ` M uv.lock`. Reconcile
   `uv.lock` first (032 Risk 5): its diff is the lock catching up with `pyproject.toml` (root
   0.29.0 → 0.46.0, `pyyaml` gone; 1 insertion, 58 deletions) — commit it on its own before this
   phase's commit, or discard it; record which. Baseline `pytest -q` from the repo root with
   `.venv/bin/python` (no install): record passed/failed counts; only the 7 repro tests fail.
   Proof: the counts in the summary; `git status --short` empty.
1. Tests first (checklist order 1). Rewrite the three dry-run tests in
   `tests/test_plan026_navigable_dry_stubs.py` on `_bare_runner({"t": object(),
   "send_chat_message": object()})` with `_pb(steps)` (the step dicts, including
   `output_schema` for the llm_step at :131, carry everything the tests need); inline `CODE`,
   `_Bus` (the `subscribe` variant, `test_versioned_specs.py:58-67`), `_Tool`, `_Tools`, `_noop`
   into `test_publish_settings.py` (replacing :30-32) and `_Bus` into `test_tool_timeouts.py:17`;
   delete `tests/test_specs.py` and `tests/test_versioned_specs.py`; make every other test edit
   listed under Scope. Proof: `grep -rln "test_versioned_specs\|from test_specs" tests/` is
   empty; `pytest tests/test_plan026_navigable_dry_stubs.py tests/test_publish_settings.py
   tests/test_tool_timeouts.py -q` green before any package change (at HEAD the PATCH `{}` →
   400 guard :889 and `set_autonomy(require_run=False)` already behave as the trimmed
   assertions expect). The edits that assert on removed code stay red until step 3 (or 5 for
   the manifest): `test_version_routes.py:198` (`mint_version` requires `source_version`,
   versioning.py:131, until step 3 — make that one edit in step 3 instead), `test_build_operate`,
   `test_delegation`, `test_delegate_prompt`, `test_card_route`, `test_zero_yaml:85`,
   `test_candidate_flow`'s gate order, `test_manifest_drift`. Record the red set at this step.
2. Rename `_spec_target` → `_resolve_target`, move it above `_dry_run`, make `_dry_run` and
   `_preflight` call it. Proof: `grep -n "_spec_target" plugin_playbooks/` empty; `pytest
   tests/test_candidate_flow.py tests/test_probes.py -q` green (the `"no candidate"` pins hold).
3. Delete the spec code, gate, routes, model, module, version copy and backfill per Scope; add
   `stubs` to `playbook_dry_run` (signature, decoding, `runner.dry_run(target,
   inputs=input_data, stubs=stub_data)`, ToolDef property `"stubs": {"type": "string",
   "description": "JSON object of scripted results keyed by step id or tool name (step id wins);
   values are the raw result payload"}`). Proof: `grep -rn "PlaybookSpec\|specs_gate\|
   run_all_specs\|spec_from_run\|spec_source_version\|copy_specs\|require_specs\|playbook_spec"
   plugin_playbooks/*.py` → 0 hits; `pytest tests/test_candidate_flow.py
   tests/test_publish_settings.py -q` green with gates `["static_validation", "test_run",
   "probes"]`.
4. DB. Add `_drop_spec_remnants(engine)` with the two count logs and the two statements; call it
   after `_drop_legacy_indexes` (:728-731) and before the per-index create loop (:737-745); in
   the same change remove `_COLUMN_MIGRATIONS` :33 and :35-36 and `_LEGACY_INDEXES` :45
   (otherwise `_ensure_columns._missing` :53-62 raises `NoSuchTableError` on the dropped table
   and no column add runs in that load); delete `backfill_spec_versions` and its call. Data
   preservation (luna `plans/EXECUTION.md` "Critical — Data Preservation"): the helper logs both
   counts BEFORE dropping, at INFO, naming the release ("playbooks: dropping playbook_specs (%d
   rows) and playbooks.publish_require_specs (%d rows false) — feature removed in 0.47.0"); the
   file export of the rows and of the `publish_require_specs` values on any real DB is the
   owner's vaselin step (M0 touchpoint), to a path outside every repo, recorded (path only) in
   the summary. Proof: the drop tests in `tests/test_no_spec_feature.py` (Exit tests) green,
   including the `caplog` assertion on the row count.
5. UI, manifest, versions. `cd ui-src && npm ci && npm test && npm run build` (`tsc -b && vite
   build`, `emptyOutDir`); commit the new hashed pair and `ui/index.html`, delete the stale pair.
   Regenerate the manifest `[[tools]]` blocks and counts from the ToolDefs (30 → 25 tools, 11 →
   10 tables); bump the three stamps to 0.47.0 — a minor bump because this phase changes the
   manifest (`[[tools]]`, `db_tables`, skills) and the UI bundle (032 Conventions); no major
   bump: 0.x scheme, and no external reader of the removed routes or evidence keys (checklist,
   verified 2026-09-07). Proof: `pytest tests/test_manifest_drift.py -q` 4 passed; `grep -c
   "require_specs\|/specs\|tests-header\|version-specs\|No tests yet"
   plugin_playbooks/ui/assets/*.js` → 0; `git status` shows exactly one new js + one new css
   under `ui/assets/` and the two old ones deleted.
6. Docs, guard, lint, suite, commit. README and vision.md edits per Scope; write
   `tests/test_no_spec_feature.py` items 1-6; `uvx ruff check --select F401 plugin_playbooks
   tests` clean; `pytest -q` full; then ONE commit on `v2-runtime` (suggested subject: `0.47.0:
   remove the specs (Tests tab) feature — master §2 Specs removal, owner 2026-09-07`), not
   pushed. Proof: every Exit test below; `git log --oneline -1` shows the single phase commit on
   top of the reconciled tree (5306f7f or the `uv.lock` commit); `git status --short` empty
   (the plan folder is already committed at 5306f7f; `execution_summary.md` is a later,
   separate plan-only commit).
7. Hand-off. Run the two cross-repo checks (below) against this commit, record their verdicts
   with the luna and dojoP shas checked (at writing luna `fix-playbooks` 5a92c05 and dojoP
   `main` 316f007, both plan-only commits on top of f05bdf2 / f51915d) and the owner's PG
   rehearsal outcome (or "deferred to before the first M1 side-load"), write
   `execution_summary.md`, and revise plugin/01-05 if anything moved (names, counts, hooks).

## Exit tests
- Full suite: `pytest -q` from the repo root — every test passes except exactly the 7 repro
  tests; `tests/test_specs.py` and `tests/test_versioned_specs.py` do not exist. Which of the 7
  red repro tests turn green: none (expected — the three lifecycle pins stay red until the P0
  plans, the four runtime pins until plugin/02-07, per 032 Conventions).
- Relocated dry-run tests, `tests/test_plan026_navigable_dry_stubs.py`:
  `test_dry_run_stub_by_step_id_and_tool_name` (step-id stub wins over the tool-name stub in
  `references[...]["result"]`, the stubbed value reaches the downstream `resolved_args`),
  `test_dry_run_stubs_agent_and_llm_steps` (`trace[0]["output"] == {"label": "urgent"}`,
  `trace[1]["output"]["resolved_args"] == {"v": "urgent"}`, the unstubbed run uses the schema
  placeholder), `test_loop_over_unstubbed_dry_output_iterates_zero_times`
  (`references["crawl"]["iterations"] == 0`, `["results"] == []`) — pass.
- Fixture importers: `tests/test_publish_settings.py` (`test_defaults_on_and_patch_route`: PATCH
  `{}` → 400, PATCH `require_run` persists, unknown name → 404; `test_tool_sets_the_flags`:
  `p.publish_require_run is False`; the two never-blocks tests;
  `test_candidate_tool_publish_run_gate_off`) and `tests/test_tool_timeouts.py`
  (publish/rollback `timeout_seconds >= 300`) pass with no cross-file test import.
- `tests/test_manifest_drift.py`: `test_toml_tools_match_code` (25 names == code),
  `test_toml_tables_match_models` (10), `test_version_stamps_agree` (`0.47.0` in
  `pyproject.toml`, `luna-plugin.toml`, `__init__.py`), `test_owner_facing_tools_lead_with_why`
  (2-tuple) — 4 passed.
- `tests/test_pblang.py::test_skill_examples_compile` — passes (≥ 2 python blocks in
  `_AUTHORING_SKILL_BODY` still compile after the section delete).
- Drop helper, in `tests/test_no_spec_feature.py`:
  `test_drop_spec_remnants_removes_table_and_column` — aiosqlite engine,
  `Base.metadata.create_all`, raw `CREATE TABLE playbook_specs (id INTEGER PRIMARY KEY,
  playbook_id TEXT)` + two inserted rows, `ALTER TABLE playbooks ADD COLUMN
  publish_require_specs BOOLEAN NOT NULL DEFAULT TRUE`; after one `await
  _drop_spec_remnants(engine)`: `inspect.has_table("playbook_specs")` is False,
  `publish_require_specs` not in `get_columns("playbooks")`, caplog contains "playbook_specs (2
  rows)"; `test_drop_spec_remnants_is_idempotent` — the second call raises nothing and logs no
  drop line; `test_fresh_db_never_creates_spec_remnants` — after `create_all` on a fresh engine
  `"playbook_specs" not in Base.metadata.tables`, no such table, no such column. PG: the owner's
  vaselin rehearsal with the row export (M0 touchpoint) — recorded in the summary, not a pytest.
- Publish gate list: `tests/test_candidate_flow.py` gate-order assertion ==
  `["static_validation", "test_run", "probes"]` with `all(g["ok"])`, and guard item 4 (the
  approval card has no "Specs" entry; `"require_specs" not in` the set_autonomy
  `ToolDef.parameters["properties"]`; raw `_set_autonomy(name=…, require_specs=True)` raises
  `TypeError`).
- Guard `tests/test_no_spec_feature.py`, green and kept forever, items exactly as the checklist's
  "The 'we forget' guard": (1) no registered tool name (`build_tools` + `build_delegation_tools`)
  matches `playbook_spec`; manifest `[[tools]]`, `requires.tools`, `db_tables`,
  `requires.tables` agree with code; (2) `Base.metadata.tables` has no `playbook_specs`,
  `Playbook` has no `publish_require_specs`, `_COLUMN_MIGRATIONS`/`_LEGACY_INDEXES` name no spec
  table or column, plus the drop tests above; (3) word-bounded token grep over
  `plugin_playbooks/**/*.py`, both skill bodies, every ToolDef description and parameter
  description, `reference.py`, `_delegate_prompt(...)` output, `card.py`, `README.md`,
  `vision/vision.md`, `plugin_playbooks/ui/assets/*.js` for `playbook_spec`, `PlaybookSpec`,
  `playbook_specs`, `specs_gate`, `spec_from_run`, `carried_from`, `require_specs`,
  `run_all_specs`, `copy_specs`, `spec_source_version`, `specsLabel`, `specsHeadline`,
  `SpecEntry`, `getSpecs`, `runSpecs`, `\bTests\b`, `all specs` → zero hits (the `\bTests\b`
  allowlist is exactly `reference.py`'s Jinja "Tests:" line once the two "Tests the CANDIDATE"
  rewordings in Scope land); plus one bare
  `\bspecs?\b` check with the documented allowlist (`reference.py` language-"spec" lines and its
  Jinja "Tests:" line, `agent_tools.py` "spec" = specification lines, `testing.py` `spec`
  parameter; `plans/` not grepped); (4) as above; (5) `handlers["playbook_dry_run"](name=…,
  stubs=json.dumps({...}))` and the dict form both script a result by step id and by tool name,
  and `"stubs"` is in the ToolDef `parameters["properties"]`; (6) keys of `_PHASE_BY_TOOL`,
  `_GATED_TOOLS`, `_GATED_TOOL_OWNER_WORDS`, `card.WAIT_WORDS` ⊆ registered tool names;
  `{n for n, (td, _) in tools.items() if td.policy == "prompt_always"} ==
  {"playbook_set_autonomy", "playbook_run_candidate"}` and that set `<= _GATED_TOOLS`
  (re-homes `test_specs.py:546-549`; equality with `_GATED_TOOLS` is wrong — it also holds the
  approval-card tools `playbook_publish`/`playbook_rollback`, delegation.py:94-100, which are
  not `prompt_always`); `set(manifest.skills[0].tools) <= set(AUTHORING_TOOLS)` (re-homes :561).
- UI: `cd ui-src && npm ci && npm test` green — `ConnectionsTab.test.tsx` (probes only),
  `VersionsTab.test.tsx` (view ids include `connections`, not `tests`; "never run → ✗" seeds
  `runs: 0`; the refusal test uses gate `probes`), `trust.test.ts` (no `specsLabel`/
  `specsHeadline`), `PublishSettings.test.tsx` (single `require_run` switch); `npm run build`
  rebuilt the bundle and the new pair is committed (grep in step 5 → 0 spec tokens).
- `uvx ruff check --select F401 plugin_playbooks tests` → "All checks passed!".
- Cross-repo: luna/00 and dojop/00 green (next section).

## Cross-repo checks
- luna/00 — luna 007.009 against this branch, no plugin-set rebuild (the marketplace re-pin and
  `scripts/build_plugin_set.py` stay in the §4 ship step): `luna/tests/conftest.py:23-32`
  inserts `LUNA_PLUGIN_SET_DIR` (default `~/.luna/plugin-set`) on `sys.path` when the directory
  exists, so create a scratch dir holding a symlink or copy of this repo's `plugin_playbooks/`
  at the phase commit and run, from `luna/`, `LUNA_PLUGIN_SET_DIR=<scratch> <luna venv> -m
  pytest tests/007.009-vibe-playbook -q` → green (`test_dry_run.py`, `test_tools.py`,
  `test_traces.py`, `test_triggers.py`, `test_validation.py`). First confirm
  `plugin_playbooks.__file__` under that env resolves to the branch, not the stale vendored
  `luna/plugins/plugin_playbooks` (0.3.0; luna/01 deletes it). The `test_tools.py:29` comment
  naming `playbook_specs` is prose; `create_all` at :30-31 simply no longer creates the table.
- dojop/00 — `cd dojoP && python run.py validate` green with 7 live keyhole tasks
  (`keyhole-failing-spec-surfaced.yaml`, `keyhole-specs-are-not-runs.yaml`,
  `spec-from-guarantee.yaml` moved to `tasks/_retired/` with `tier: retired` +
  `retired_reason`) and the new `lib/tasks.py::_validate` rule that no live task grades
  `playbook_spec_*`. Owned by dojop/00; this phase records the verdict and the dojoP commit sha.
- Owner PG rehearsal (M0 touchpoint, `vaselin-*` only): export `playbook_specs` rows and
  `playbooks.publish_require_specs` values to a file outside the repos; side-load this build;
  the load log shows the two "dropping" lines with counts; restart → no drop lines; run one v1
  playbook. A PG-only failure reopens this phase.

## Risks and open questions
1. Assumption: `uv.lock` is reconciled as its own commit (or discarded) before this phase's
   single commit (032 Risk 5); after the 0.47.0 bump the lock's root `version` is stale again —
   `uv lock` (no install) inside the phase commit, or leave it and say so in the summary. No
   test pins it.
2. Assumption: `_dry_run` delegates to `_resolve_target` in the same commit (the checklist marks
   it optional). Safe: the three `"no candidate"` pins in `test_candidate_flow.py` match
   `_resolve_target`'s wording and the JSON error shape is unchanged.
3. Assumption: the guard's bare-word allowlist (item 3) is keyed by file + regex context, not by
   the checklist's line numbers, which shift after the deletions (`reference.py:3/:121`,
   `agent_tools.py:113/:1517`, `reference.py:110`, `testing.py:44-47` at HEAD). The two
   "Tests the CANDIDATE" rewordings (`__init__.py:220`, `agent_tools.py:1645`) are this plan's
   addition — the checklist lists neither; without them the `\bTests\b` token check has two
   non-spec hits and needs a second allowlist entry.
4. Assumption: the row count is logged inside the helper (INFO) and the file export is the
   owner's step on the vaselin agent — no in-repo export code, nothing written under any repo.
   The suite cannot exercise PG; SQLite ≥ 3.35 is required for DROP COLUMN (`.venv`: 3.50.4) —
   if the executing interpreter is older the drop tests must skip with that reason, not fail.
5. Assumption: the moved tests live in `tests/test_plan026_navigable_dry_stubs.py` (checklist)
   and the UI replacement test is `__tests__/ConnectionsTab.test.tsx` (the checklist names no
   file); `ui-src/package.json` `version` 0.4.1 is not one of the three stamps and is left alone.
6. Not verified during plan writing: `npm ci`/`npm test` were not run (`node_modules` absent;
   installs forbidden in the planning session); UI hooks were checked on source only.
   Assumption: vitest is green at HEAD (UI sources identical at HEAD and 749f126, checklist).
7. Scope check against the master decomposition: no misassignment found — every dojoP and luna
   hook is owned by dojop/00, luna/00, luna/01 or luna/04 as the checklist assigns; this phase's
   only cross-repo duty is to run and record the two checks.
8. The `_status` replacement sentence deliberately names no `playbook_dry_run` parameter;
   plugin/08 appends the `stubs_from_run` sentence when it exists. The commit subject in step 6
   is a proposal (master silent).
9. Rollback safety is real but empty: an older build re-creates the table (`checkfirst=True`)
   and re-adds the column from its own `_COLUMN_MIGRATIONS` with no data — the owner's export is
   the only copy.

## Execution summary
Written to execution_summary.md in this folder after the phase runs, using this template:
- Ran: (commands, dates, HEAD before/after, results folder ids)
- Results: (every exit test with its outcome and the relevant output; anything red and why)
- Deviations from this plan: (what changed and why)
- Learned: (facts that change later phases)
- Revised: (which later phase files were edited because of this, and how)
