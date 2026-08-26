# Phase 4 — Specs engine: execution summary

Shipped as **0.11.0**. Specs are stored tests for a playbook: fixture
`inputs`, scripted `stubs` for effectful steps, and `expect` assertions
evaluated against a dry-run trace. They auto-run on every candidate save
(reported, non-blocking) and are a blocking promote gate.

## What was built

- **Runner stub seam** (`runner.py`): `dry_run(playbook, inputs=, stubs=)`.
  In dry branches only: a tool_call whose step-id or tool-name appears in
  `ctx.stubs` returns the scripted value as its `result` (step-id wins);
  agent/llm steps replace their whole output. Subtask and parallel child
  contexts inherit the parent's stubs. Live runs never see stubs.
- **`specs.py`** (new): `SpecDef`/`SpecExpect`/`ToolExpect` (pydantic,
  extra=forbid), `parse_spec_yaml` with readable errors, pure
  `evaluate_spec(spec, dry_result)` (status, steps_ran order restricted to
  named steps, steps_not_ran, per-tool count + args_contain subset/substring
  match, output_contains, error_contains), `run_all_specs` (DB glue shared
  by save auto-run / spec_run tool / both promote gates / REST run-all;
  updates the last-result cache, caller commits), and `spec_from_run`
  (record & replay proposal — stores nothing).
- **Model**: `playbook_specs` table (unique per playbook+name, JSONB spec,
  `last_result`/`last_run_at`/`last_version` cache).
- **Tools** (all skill-gated + in AUTHORING_TOOLS): `playbook_spec_add`
  (upsert + immediate run, warns if stored-but-failing), `playbook_spec_list`,
  `playbook_spec_delete` (prompt_always), `playbook_spec_run`
  (auto/candidate/live/number targeting), `playbook_spec_from_run`
  (YAML proposal from a recorded run, default latest done run).
- **Integration**: candidate save auto-runs specs and embeds
  `specs: {passed, failed[, failures]}` with fix-or-update-spec steering;
  promote gate "specs" between static_validation and manifest_drift in BOTH
  the tool and REST paths (tool refusal names the gate + failing specs; REST
  raises 422 with the same fields); new REST `GET .../specs` and
  `POST .../specs/run` (feeds the phase-6 Tests tab).
- **Skill**: new SPECS section (anatomy, auto-run + gate, prefer
  spec_from_run after a good run, "a playbook with no specs has no safety
  net", keep specs small).

## Verification

- **Unit**: 125/125 (18 new in `tests/test_specs.py` — real
  PlaybookRunner for the stub seam and tool flows, pure-function evaluator
  matrix, promote refusal/pass, from_run round-trip; 1 phase-3 assertion
  updated for the new gate list).
- **Live QA (:8766, real agent turns)**:
  - Turn F: agent created spec `greets-with-name` via playbook_spec_add
    (stubbed llm step, args_contain assertion) — stored, ran against live
    v4, passed; row + expect JSON verified in DB.
  - Turn G: agent broke the notify message. The **phase-2 manifest gate
    refused the plain edit** (drift vs manifest); agent used
    playbook_edit_force → candidate v6 saved with
    `specs: {passed: 0, failed: 1}` and the exact diff line
    (`expected ... 'Hello QA friend', saw [{'message': 'static goodbye'}]`).
    Promote (approved) was **refused with gate "specs"** naming the spec;
    live untouched (6|4|6); last_result cache stamped `6|false`.
  - Turn H: candidate fixed (v7), spec_run against candidate passed,
    promote cleared `static_validation → specs (1/1 passed) →
    manifest_drift`; DB 7|7|NULL, v7 has promoted_from.
  - Turn I: playbook_spec_from_run pinned a proposal from the real phase-3
    run (inputs, llm + tool stubs, expect with order and tool count) without
    storing anything.
  - REST `GET /playbooks/qa-code-hello/specs` and `POST .../specs/run`
    returned correct payloads (ran_against_version 7, 1/1 passed).

## Surprises / notes

- The manifest-drift-at-edit gate (phase 2) fired before the spec machinery
  could even be exercised in Turn G — the layers compose: manifest guards
  intent, specs guard behavior. `playbook_edit_force` exists as the escape
  hatch and the spec gate still caught the forced change at promote time.
- Tool results recorded in step outputs are sometimes JSON *strings*;
  `spec_from_run` passes them through as-is into stubs. Harmless (stubs are
  opaque payloads) but proposals may contain quoted JSON — the skill already
  tells the agent to trim proposals.
- PHASE.md's original stub example wrapped values in `result:`; implemented
  semantics are that the stub value IS the result payload. PHASE.md fixed.
- test_candidate_flow's recording `_Runner` stub needed no change: with no
  specs stored, `run_all_specs` never calls `dry_run`.

## Reassessment of remaining phases

- **Phase 5 (probes)**: unchanged. Probes plug into the same gate list
  (a `probes` entry after `specs`) and can reuse `run_all_specs`'s shape.
  The `_shim_playbook`/`_shim_for` helpers and last-result cache pattern
  carry over directly.
- **Phase 6 (UI)**: cheaper than planned — `GET .../specs` already returns
  spec bodies + last results, and promote's gates array is UI-ready. The
  Tests tab is mostly rendering.
- **Phase 7 (cleanup)**: unchanged; still includes the stale July pending
  approvals sweep. Add: consider normalizing step-output tool results to
  dicts (the JSON-string observation above) if it bothers the UI.
