# 015 execution summary — plugin-playbooks 0.26.0

Implemented 2026-08-29. 265 tests green (`python -m pytest tests/`).

## Changes by file

- **models.py** — `Playbook.publish_autonomy` (String(16), default 'ask');
  `PlaybookRun.report_to` (UUID, nullable) + `PlaybookRun.is_test`
  (Boolean, default False); new `PlaybookFixProposal` table
  (`playbook_fix_proposals`, indexed on (playbook_id, signature), status
  open|approved|dismissed|resolved, failure_count, last_run_id,
  approval_id).
- **publish.py** (new) — shared publish contract: feature-detected core
  accessors (`ops_conversation_id`, `conversation_kind`,
  `conversation_state`), `latest_run_evidence`/`test_run_gate` (green run
  of the exact version after `PlaybookVersion.created_at`; pre-0.26
  evidence accepted via `trigger == "agent-candidate"`), and
  `announce_publish` (emits `playbook.published` + muted ops-chat
  announcement; never raises).
- **agent_tools.py** — `playbook_promote` → `playbook_publish`;
  `_do_publish(name, version, action)` is the single gated live-path
  (candidate publish, `version=N` restore, rollback). Gate order:
  static_validation → specs (candidate only) → test_run → manifest_drift
  → probes. `_rollback` resolves the target via `promoted_from` lineage
  then delegates to `_do_publish(action="rollback")`.
  `playbook_run_candidate` passes `is_test=True`. `playbook_set_autonomy`
  gained `publish_autonomy`. All ToolDefs declare `modes=` (dropped
  harmlessly by the pre-089 SDK). `build_tools` takes optional `ctx`.
- **runner.py** — `_create_run` stamps `report_to`/`is_test` per the §1
  rules; `_drive_run` delivers to `report_to or conversation_id`, pins via
  the `ctx.pin_conversation` seam when the core has one, and
  `playbook.run.completed` now carries playbook_id/playbook_version/
  is_test.
- **triggers.py** — `background=True` subscriptions (TypeError fallback);
  single-flight dedupe keyed on playbook|event|canonical-inputs, cleared
  by the run task's done_callback.
- **fix_proposals.py** (new) — `FixProposalService` + `failure_signature`;
  files/dedupes proposals off `playbook.run.completed`, posts approval
  cards to the ops chat, wakes it on approval. Own-task dispatch so a
  parked approval can never block the bus or `_complete_run`.
- **routes.py** — promote/rollback REST routes run the same
  `test_run_gate` and call `announce_publish` (endpoint paths unchanged
  for the UI); `init_routes` takes `ctx`.
- **__init__.py** — column migrations for the three new columns; failure
  digest excludes `is_test` runs; `prompt_sections(kind, state)` with
  ops-mode sections and per-state MUST-rule handling; FixProposalService
  wired in on_load/on_unload; skill/prose renamed promote → publish;
  version 0.26.0 (all three stamps).
- **card.py / reference.py / validation.py / specs.py / probes.py /
  README.md** — agent- and owner-visible "promote" wording renamed;
  card WAIT_WORDS keys off the new tool name.

## Bug found while testing

`test_run_gate` originally read `run.error` — `PlaybookRun` has no error
column (errors live on `PlaybookStepRun`). The failed-test refusal now
queries the failed step's error.

## Tests

- Updated: candidate_flow, specs, probes, card_route, waiting, delegation,
  code_tools, failed_run_awareness (rename + green-run evidence via
  `tests/evidence.py`).
- New `tests/test_build_operate.py`: gate refuses untested/red candidates
  and stale evidence, passes green with announcement; report_to stamping
  matrix (6 cases); digest ignores test runs; trigger single-flight
  dedupe + release + input-sensitivity; mode declarations (skips on
  pre-089 SDK); fix-proposal dedupe, test/stale-run skips, signature
  stability.

Deviations from 089: see PLAN.md §"Recorded deviations" (7 items).
