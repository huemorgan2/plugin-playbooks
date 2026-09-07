# 032 — v2 runtime: playbooks become real Python

Status: approved — owner, in session, 2026-09-07, via the master plan luna-fixer plans/2026-09-06-fix-playbooks/PLAN.md (Status: approved); execution not started

Written 2026-09-07. Mirror of the master plan for this repo (luna-fixer
CLAUDE.md "Fixing discipline"); the master stays the authority, and phase
detail lives in `phases/NN-<slug>/PLAN.md`.

## Baseline

- Branch `v2-runtime` @ 8c31a60 (origin/main 749f126). 8c31a60 adds only the
  two red repro test files; every non-test line is identical to origin/main.
- Version 0.46.0 in all three stamps: `pyproject.toml:3`,
  `plugin_playbooks/luna-plugin.toml:2`, `plugin_playbooks/__init__.py:624`
  (`PluginManifest(version=...)`), pinned by
  `tests/test_manifest_drift.py:69-78 test_version_stamps_agree`.
- Manifest at HEAD: `[requires] tools = 30`, `tables = 11`
  (`plugin_playbooks/luna-plugin.toml:36-37`); `db_tables` (:8) still lists
  `playbook_specs`.
- Tests: 42 `tests/test_*.py` files plus `conftest.py`, `evidence.py`,
  `readstage.py`, flat in `tests/`; 403 green + 7 red repro tests (master §6),
  the 7 mapped under "Conventions used".
- UI: sources in `ui-src/src`, built by `ui-src/vite.config.ts` into
  `plugin_playbooks/ui` (`outDir: '../plugin_playbooks/ui'`, `emptyOutDir`);
  the committed bundle is `plugin_playbooks/ui/assets/index-BqhDbTui.js` +
  `index-BgLNZxTK.css`, referenced from `plugin_playbooks/ui/index.html:7-8`.
  `ui-src/node_modules` is absent locally (`npm ci` first).
- Working tree at writing time: `uv.lock` carries an uncommitted change
  (1 insertion, 58 deletions) that predates this plan — see Risks.
- Jail: plugin-inline-code-run 0.4.0, managed install only
  (`~/.luna/managed_plugins/plugin_inline_code_run`, `backends.py:33
  PY_FLAGS`, spawn :129). The suite never reaches it
  (`tests/test_plan004_code_and_functions.py:238` asserts the install hint).

## Why

Master §0: the owner finds the agent slow and wrong at authoring playbooks
while it is good at Python; pblang is Python-shaped syntax with non-Python
semantics and the plugin carries a catalog of the model's wrong guesses
(`reference.py:120-138 LANGUAGE_MINIREF`, "Rules agents forget" :134-137).
Master §1 findings that live in this repo: no resume — `sweep_orphaned_runs`
(runner.py:317) fails every in-flight run on restart (§1.1);
`_run_wait_for_approval` auto-approves (runner.py:1060-1069) and
`_run_wait_for_event` returns a stub (:1071-1083) (§1.2); tool steps await
`rt.handler(**call_args)` with no timeout (runner.py:877) (§1.3); vault refs
resolve on a copy, rows keep the ref (`_resolve_vault_refs`, runner.py:759)
(§1.4); `Playbook.code` already exists (models.py:40-44) (§1.12); the
13-item contract surface that must keep working in any format (§1.13). The
specs (Tests tab) feature goes first, on the owner's decision of 2026-09-07
(master §2 Specs removal; checklist
`luna-fixer plans/2026-09-06-fix-playbooks/specs-removal.md`).

## How this plan executes

- Phases run in folder order, 00 → 12. Each has `phases/NN-<slug>/PLAN.md`
  (written before any code of that phase; it is the repo skill's PHASE.md
  and ends with the template of its execution summary) and, after it runs,
  `phases/NN-<slug>/execution_summary.md` (files, commit, version; what was
  verified and how; deviations; surprises; cross-repo verdicts). Summaries
  are created only after the phase runs, never empty up front.
- Loop per phase: execute → run the phase's exit tests AND the full suite
  (`pytest` from the repo root; UI phases also `cd ui-src && npm ci && npm
  test && npm run build`) → write `execution_summary.md` → re-read every
  later phase file against what this phase taught and revise it (recorded
  in the summary under "Reassessment of remaining phases"; edited lines in
  the later file carry the phase that changed them). Phase N+1 never starts
  before phase N's summary exists.
- Stop rules (write the summary, name the blocker, wait for the owner): the
  existing suite goes red for a reason the phase does not explain; phase
  05's bench go/no-go fails the STOP RULE (re-plan before 06); phase 11's
  keyhole gate is red; a master §3 Success criterion is missed at a measured
  phase (05, 11, 12); phase 02's A/B decision lands on option B (a change in
  another repo — surface it, do not implement it here); any owner decision
  a phase file flags as open.
- Master vs phase file conflicts: the master wins; the phase file is
  corrected and the correction recorded in the summary.

## Phase index

| NN | Title | Master | Depends on | Exit gate |
| --- | --- | --- | --- | --- |
| 00-specs-removal | Specs (Tests tab) removal | M0 (§2 Specs removal, §3 P1 step 0, specs-removal.md) | — (P0 plans are master prerequisites, not phase inputs) | One commit; suite green minus deleted files; manifest tools 25 / tables 10 + three stamps agree (`tests/test_manifest_drift.py`); `tests/test_no_spec_feature.py` green; `_drop_spec_remnants` test on a seeded aiosqlite DB (+ owner PG trial with the row export); gate list == `[static_validation, test_run, probes]`; `npm test` green, bundle rebuilt + committed; `uvx ruff check --select F401` clean; luna/00 and dojop/00 green |
| 01-contract-and-checker | v2 contract doc, static checker, format sniff | M1 | 00 | `docs/v2.md` written; `tests/test_v2_checker.py`: one test per rule, all issues at once, every issue carries `example_fix`, line numbers match the saved code, the §2 example passes clean; sniff/precedence table incl. the mismatch error; doc↔checker sync test; existing suite green |
| 02-shim-and-segment-loop | In-jail shim, host segment loop, ctx.tool/now/random/log, error contract, hash-seed pin | M1 | 01 | `tests/test_v2_loop.py`: 3-effect playbook completes through the real jail with an identical journal on re-run; 20-50 strings iterated around effects replay identically across 5 spawns (decides option A vs B — recorded in the summary and in luna-fixer `inline-code-run-plan.md`); vault ref raw in the journal, resolved at execution; `_timeout` per effect; MAX_EFFECTS trips loudly; `error.json` carries playbook line + last effect + locals and lands on the run row; timeout names the last effect; divergence raises the typed error; per-segment spawn latency recorded |
| 03-llm-agent-subtask-gather-approve | Effects: ctx.llm, ctx.agent, ctx.subtask, ctx.gather, ctx.approve (in-process) | M1 | 02 | `tests/test_v2_effects.py`: each kind end-to-end through the loop on the v1 suite's `run_llm`/`run_turn` fakes; gather order + first failure after all settle; subtask returns the child's value, cycle guard trips; approve blocks / `ctx.Rejected` / `ctx.ApprovalExpired`; agent transcript on the journal entry; a handled failure is `failed_handled` and the run completes |
| 04-lifecycle-corrections | format column + tool wiring, propose = candidate, loud intake, edit-error payload, error surfacing, interim UI | M1 | 01, 02, 03 | propose → `candidate_saved` exact shape, `_live_version_of` None, `playbook_run` refuses naming the candidate, re-create no longer promotes; `"4"` → 4 on chat + trigger paths, `"abc"` fails at intake naming the input; checker error keeps the ticket (`ticket_still_valid`), green write returns `validated: true`; compute error visible in `playbook_run`/`playbook_status`/digest, `failure_signature` dedupes; format precedence at tool level, riders absent on python paths; interim UI test; full suite green |
| 05-dry-run-skill-and-go-no-go | Dry run on the same loop, the v2 skill, the bench go/no-go | M1 | 03, 04 | `tests/test_v2_dry_run.py` (once-iterating DryStub, `unreached_call_sites`, `DryStubError` text, per-occurrence stubs, `simulated` / `simulated_nothing_exercised`, no run rows, loud intake on the dry path); `tests/test_v2_skill.py` (size bound, both examples compile + dry-run, THE LOOP honesty rules verbatim); go/no-go verdict from dojop/01 recorded with the results folder id; STOP RULE applied |
| 06-durable-journal-and-resume | Durable journal tables, write-ahead in_flight rows, resume on on_server_ready, OutcomeUnknown | M2 | 02, 05 (STOP RULE passed) | `tests/test_v2_resume.py`: kill mid-run → restart → resume with the same journal prefix (headline); kill between in_flight write and result write → tool NOT re-executed, row `timed_out_unknown`, segment gets `ctx.OutcomeUnknown`; gather + subtask across restart; v1 runs still swept, v2 running rows not; manifest tables 11 |
| 07-parked-runs | Parked runs: ctx.approve park form, real ctx.wait_event, max_duration, cancel | M2 | 06, luna/02 | `tests/test_v2_parked.py`: approval decided while the server was down → resume on `on_server_ready`; reject / expiry exceptions catchable; wait_event fires across a restart, times out with `ctx.EventTimeout`, rejected by the checker without `timeout=`; cancel releases card + subscription; `max_duration` fails the park loudly; `playbook.run.parked` emitted; `playbook_status` says "parked on approval #N — nothing to poll" |
| 08-lifecycle-integration-and-parity | Lifecycle integration and v1/v2 parity | M3 | 05, 07 | Full suite green; `tests/test_v2_parity.py` (wake, fix proposals, failure digest, trust badges, test_run gate identical for a v2 run; completed payload = the HEAD key set (12 at 8c31a60, runner.py:1474-1490) + any keys phases 02-07 added + `result`); live_version-writer invariant test red on a fixture adding a writer; `stubs_from_run` dry run reaches the corrected line and lists unreached sites; per-run card for `agent_must_confirm` (v2 parks at effect 0, v1 awaits in the task), result never names `playbook_set_autonomy`, gate text names a parked candidate run, test-run cards labelled |
| 09-provenance-and-overview | Result provenance envelope and playbook_overview | M4 | 07, 08, luna/03 | `tests/test_v2_provenance.py` (envelope first in key order on all five tools; dry run status `simulated`); `tests/test_v2_overview.py` (fresh / candidate-only / parked fixtures, caps + `more: N`, next hints); `test_manifest_drift` green with 26 tools; existing suite green |
| 10-canvas | Canvas: server-side graph endpoint, compute/error_boundary nodes, run-trace overlay | M4 | 04, 06, 08 | `tests/test_v2_graph.py` snapshots (node ids stable under a non-call-site edit; a failed run's trace lands on the failing call-site node with the error); route test (version selection, 404); `npm test` green incl. the new node kinds, v1 canvas tests unchanged; owner visual check on a vaselin-* agent recorded |
| 11-delegation-v2-and-keyhole-gate | Delegation v2 prompt, candidate-conflict guard, author stamping, end-to-end script, keyhole gate | M4 | 04, 08, dojop/02 | `tests/test_v2_delegation.py` (conflict guard names the author; author stamp on the row + versions UI; 11 sections unchanged, `test_delegate_prompt` green); end-to-end script green (no validate after a green write; publish gated by a card); keyhole verdict from dojop/02 recorded; existing suite green |
| 12-migration-and-final-measurement | Migration of live pblang playbooks and the final measurement | M5 | 08, 11, dojop/03 | Count of live pblang playbooks recorded (source named); per migrated playbook the ledger row is complete (dry run with `stubs_from_run` matches the last green live run — helper output attached — real candidate run green, owner publish card approved, live version stamped); final measurement verdict against every Success criterion recorded; suite green, v1 runner untouched |

## Cross-repo map

Ids: plugin/NN = this repo's phases; luna/NN = luna
`plans/107-fix-playbooks/phases` (branch `fix-playbooks` @ f05bdf2,
origin/main 86aaf68); dojop/NN = dojoP `plans/0002-fix-playbooks-bench/phases`
(main @ f51915d); M0-M6 = luna-fixer `plans/2026-09-06-fix-playbooks/phases/`.

- plugin/00 (M0) ← luna/00 (007.009 suite run against this branch via
  `LUNA_PLUGIN_SET_DIR`, luna `tests/conftest.py:23-32`; the plugin-set
  re-pin waits for M6) and dojop/00 (`python run.py validate` with the three
  spec tasks retired + the no-live-spec-task rule). luna/01 (vendored copy
  deletion) is independent.
- plugin/02 (M1) decides the hash-seed pin: option A (shim re-exec, this
  repo only) or option B (`hashseed=` runner option in plugin-inline-code-run
  0.4.0). Recorded in the phase-02 summary and in luna-fixer
  `plans/2026-09-06-fix-playbooks/inline-code-run-plan.md`.
- plugin/05 (M1) → dojop/01: twin tasks `authoring-stateful-queue-v2`,
  `editing-cross-cutting-v2` + `authoring-within-budget`, ≥ 5 trials vs
  results/0055-run, against this phase's build. The verdict gates plugin/06.
- plugin/07 (M2) ← luna/02: kind check in `_on_approval_orphan_decided`
  (luna `plugins/plugin_api/app.py`) so `playbook_effect` orphans never spawn
  the "re-issue that call" turn; the plugin resumes on `approval.decided`
  (luna `approval/db_impl.py:425/:671/:877/:883`, `in_memory_impl.py:227/:233`).
  Tested against the luna `fix-playbooks` checkout until it lands.
- plugin/09 (M4) ← luna/03: `playbook_overview` in the automation group
  (luna `agent/tool_groups.py:129-137`); the phase file states the gating
  semantics before that lands.
- plugin/11 (M4) → dojop/02: 7 live keyhole tasks in one run, ≥ 2 trials,
  pass^k ≥ 1.00, zero honesty violations, wrong-claim counts code-graded.
- plugin/12 (M5) → dojop/03 (final measurement) and M6 / luna/04 (publish,
  re-pin `luna/plugin-set.toml:63-65` — 0.39.0 today — build, promote,
  verify). Nothing in this repo ships before M6.

## Constraints

- No auto-ship. Nothing is published to the marketplace, pinned into the
  plugin set, pushed to origin, or promoted to any agent until the owner
  approves the final state (master §4 Ship). All phase commits land on the
  local branch `v2-runtime` and are not pushed; this suspends step 4 "Ship"
  of the repo's phased-execution skill (push + publish per phase) for the
  whole plan.
- Trying a phase: the owner side-loads the branch build onto a `vaselin-*`
  agent; only `vaselin-*` machines may be created, restarted, promoted, or
  deleted. v1 playbooks on that agent must keep working at every phase.
- Secrets: `luna-plugins/.env` and every key/token are never committed,
  printed, or copied into plans, summaries, journals, or test fixtures;
  journal rows and run rows store raw `vault:` refs only.
- Other repos: luna commits stay on the local `fix-playbooks` branch (not
  pushed); dojoP may commit to main and push to origin `novalystrix-org/dojoP`;
  luna-fixer commits are local-only. This repo never edits another repo.
- Owner decisions in force: "deterministic" = replayable code, not an LLM
  (randomness via `ctx.random`, journaled); the canvas stays with the same
  look; versions, promotions and runs stay; the specs (Tests tab) feature is
  removed entirely (vision.md item 5 signed off 2026-09-07); dry run, the
  `test_run` gate and probes stay; `ctx.sleep` is deferred.

## Conventions used

- Numbering: `plans/NNN-<slug>/PLAN.md` (032 is the next free number after
  031); phases in `plans/032-v2-runtime/phases/NN-<slug>/PLAN.md` +
  `execution_summary.md`. House style: title line `# NNN — title`, then
  `Status:`, then free-form sections.
- Branch `v2-runtime`; one commit per phase at minimum, phase 00 exactly one
  commit before any v2 code.
- Versions: three stamps must agree (`pyproject.toml:3`,
  `plugin_playbooks/luna-plugin.toml:2`, `plugin_playbooks/__init__.py:624`).
  A phase that changes the manifest (`[[tools]]`, `db_tables`, skills) or the
  UI bundle bumps the minor; other phases may batch into the next bump and
  say so in their summary. Manifest `[[tools]]` and counts are regenerated
  from the ToolDefs (`tests/test_manifest_drift.py:50-66`).
- Tests: flat in `tests/`, new files `tests/test_v2_<topic>.py`. Harness per
  `tests/conftest.py`: a fake `luna_sdk` is installed before import
  (declarative base, `ToolDef`/`SkillDef` kwargs holders, `message_source`
  ContextVar); each test builds its own `create_async_engine("sqlite+aiosqlite://")`
  + `Base.metadata.create_all`; the registry is a duck type with
  `.get(name).handler`, the bus a `_Bus` with `emit`/`subscribe`;
  `evidence.green_run` and `readstage.parse_read_stage` are shared. Tests
  needing the real jail are marked and documented in phase 02.
- UI: `cd ui-src && npm ci && npm test && npm run build`; the rebuilt hashed
  pair under `plugin_playbooks/ui/assets` and `ui/index.html` are committed,
  the stale pair deleted (`emptyOutDir`).
- DB changes: no migrations dir; `on_load` creates tables with
  `checkfirst=True` (`__init__.py:717-719`), `_ensure_columns` adds columns
  from `_COLUMN_MIGRATIONS` (ADD only, :49-67), `_drop_legacy_indexes`
  (:70-84); phase 00 adds the only drop helper (`_drop_spec_remnants`).
  `PlaybookRun` has no error column at HEAD (models.py:134-178); phase 02
  adds `error`, `error_type`, `traceback`, `failed_at` via `_COLUMN_MIGRATIONS`.
- Proposed v2 module layout (a proposal the executor may adjust, recording
  any change in the execution summary; every phase file uses these names so
  the phases agree): `plugin_playbooks/v2/__init__.py`;
  `plugin_playbooks/v2/checker.py` (AST checker, `sniff_format`, issue shape);
  `plugin_playbooks/v2/shim.py` (the in-jail file copied into each run dir);
  `plugin_playbooks/v2/journal.py` (`JournalStore` interface + in-memory
  store; phase 06 adds `plugin_playbooks/v2/journal_db.py`);
  `plugin_playbooks/v2/loop.py` (host segment loop + effect execution);
  `plugin_playbooks/v2/dry.py` (`DryStub`, `DryStubError`, stubs);
  `plugin_playbooks/v2/skill.py` (v2 skill text); `plugin_playbooks/v2/graph.py`
  (canvas graph, phase 10); `docs/v2.md` (the contract doc, phase 01);
  tests named `tests/test_v2_<topic>.py`.
- The 7 red repro tests (master §6):
  - `tests/test_repro_fixplaybooks_runtime.py::test_interrupted_run_survives_restart_instead_of_failing`
    → plugin/06 (resume on `on_server_ready`; v2 runs leave the sweep).
  - `…runtime.py::test_wait_for_approval_actually_gates` → plugin/03
    (in-process `ctx.approve` blocks), completed by plugin/07 (park form).
  - `…runtime.py::test_wait_for_event_actually_waits` → plugin/07.
  - `…runtime.py::test_tool_step_timeout_is_enforced` → plugin/02
    (`_timeout` per effect; v2 only).
  - `tests/test_repro_fixplaybooks_lifecycle.py::test_publish_success_carries_verified_readback`
    → P0 plan `2026-09-06-playbook-publish-verify`; stays red here.
  - `…lifecycle.py::test_approved_then_regated_same_payload_trips_loop_guard`
    → P0 plans `-playbook-publish-verify` §2 and `-approval-always-grant-hole`;
    stays red here.
  - `…lifecycle.py::test_manifest_set_does_not_flip_live` → P0 plan
    `2026-09-06-manifest-set-live-bypass`; stays red here.
  The four runtime pins target v1 `definition` playbooks; the phase closing
  a hole says whether it re-targets the pin at a python-format playbook or
  adds a v2 twin, and what stays red as a v1 pin (Risks 4).

## Out of scope

- `ctx.sleep` (durable timer) — deferred to a separate plan (master §3
  Deferred); the checker rejects it in every phase.
- The trust model: routing effects through the chat policy/approval gate,
  multi-process trigger dedupe, the owner REST publish path (master §5).
- A pblang → Python transpiler (cut; the migration is an agent rewrite
  checked by dry run against a recorded live run + a real candidate run).
- The four P0 plans (`2026-09-06-approval-always-grant-hole`,
  `-playbook-publish-verify`, `-agent-approval-visibility`,
  `-manifest-set-live-bypass`) — prerequisites with their own approval.
- Retiring the v1 runner and `LANGUAGE_CHEATSHEET` — a separate owner
  decision once zero pblang playbooks remain (plugin/12 hands it over).
- Marketplace publish, plugin-set pin, image build and fleet promotion
  (M6 / luna/04); luna-service (zero hooks).

## Risks and open questions

Assumptions made where the master is silent; each is confirmed or corrected
in the summary of the phase that meets it.

1. `luna-fixer plans/2026-09-06-fix-playbooks/inline-code-run-plan.md` (master
   §4) does not exist at writing time; phase 02 creates or updates it.
   Corrected by phase 02: the file existed (luna-fixer `687e4ad`); phase 02
   filled its §6 decision record (option A) and set it `withdrawn` at
   luna-fixer `ea38242` — nothing was created.
2. Phase layout is `phases/NN-<slug>/PLAN.md` + `execution_summary.md`, not
   the repo skill's `phase-N-<slug>/PHASE.md`; the skill's per-phase push +
   publish step is suspended by the no-auto-ship rule.
3. Versions: phase 00 stamped 0.47.0 at 18b9ebe (manifest counts, UI change); later
   phases bump per the rule above; the ship version is fixed at M6.
4. Repro-test flips: a runtime pin counts as flipped when the identical
   assertion passes against v2; the v1-shaped original stays red until the
   v1 runner is retired unless the phase re-targets it. The P0 lifecycle
   pins seed `playbook_propose` → live v1 + candidate v2; plugin/04's
   propose = candidate changes that fixture, so plugin/04 adjusts their
   setup (publish v1 through the gate first) without weakening assertions.
5. The uncommitted `uv.lock` change is not part of any phase; it was NOT
   reconciled before phase 00's commit (standing rule: left as ` M uv.lock`,
   never staged) and stays that way until the owner decides; after 0.47.0 its
   root version is stale again.
6. Real-jail tests skip (not fail) without the managed install; phase 02
   documents the harness.
7. The dojoP `*-v2` twin tasks do not exist at f51915d; dojop/01 creates
   them before the phase-05 go/no-go.
8. `playbook_language_reference` stays as the pblang reference tool; the v2
   skill is the v2 reference (master §2 Prompt surface), so tool counts run
   30 → 25 (phase 00) → 26 (phase 09).
9. Phase 12 rehearses on a `vaselin-*` agent before M6; the fleet migration
   happens after M6 under the same approval, one owner publish per playbook
   — owner to confirm.
10. The master's `plugin_api/app.py` resolves to `luna/plugins/plugin_api/app.py`
    (symbol verified by grep, lines not re-read); luna/02 owns exact lines.
