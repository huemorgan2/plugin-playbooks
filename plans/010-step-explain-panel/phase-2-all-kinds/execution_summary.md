# Phase 2 — execution summary

## What shipped

Version **0.18.0** (all three stamps: `plugin_playbooks/__init__.py` manifest,
`plugin_playbooks/luna-plugin.toml`, `pyproject.toml`).

- `ui-src/src/playbooks/explain/registry.tsx` — the explainer registry is now
  **total**: all 12 step kinds have a dedicated renderer (phase 1 shipped
  tool_call + code; this phase added agent_step, llm_step, condition, loop,
  parallel, wait_for_event, wait_for_approval, subtask, state, halt). Also
  added here: `DataFlow` (derived Reads/Writes chips; `steps.*` read chips are
  buttons that jump to the referenced step) and `FooterChips` (retry,
  on_error≠abort, timeout/timeout_seconds).
- `ui-src/src/playbooks/explain/dataflow.ts` — pure derivation: `stepReads`
  (template refs pulled recursively from every expression-bearing field),
  `stepWrites` (`steps.<id>.result`, loop `collected`, subtask `returns` keys,
  state vars incl. `into`), `findStepById` (walks then/else/body/branches).
- `ui-src/src/playbooks/PlaybookEditor.tsx` — legacy `stepDetailRows` +
  `fmtStateOp` deleted (the JSON.stringify fallback is gone); the panel now
  renders `<Explainer>` unconditionally plus `<FooterChips>` and `<DataFlow>`;
  new `onJumpToStep` prop resolves an id against the shown definition
  (candidate or live) and selects that step.
- `ui-src/src/playbooks/types.ts` — added `timeout?: number` (backend StepDef
  has both `timeout` and `timeout_seconds`).
- Tests: `explain/__tests__/explainers-kinds.test.tsx` (render tests for the
  10 new kinds, zero-data assertions, nested-step click, chip jump, footer
  defaults) and `explain/__tests__/dataflow.test.ts` (reads/writes/find unit
  tests). Suite: 87 vitest tests (was 55 after phase 1).

Commit: this commit (single commit for the phase). Phase 1 (f7e4bcf) shipped
without a version bump; phases 1+2 ship together as 0.18.0.

## Verification

- `npm run build` clean (tsc + vite), vitest 87/87, pytest 181/181 (regression).
- **Real Luna** (QA instance, port 8766, plugin 0.18.0-to-be synced into its
  managed dir and the server restarted): created playbook
  `qa84-explain-allkinds` — one step of every kind — via the API, then drove
  the editor through CDP Chrome (:9222). Verified per kind: correct headline,
  per-kind sections rendered only from the definition, footer chips (retry
  2×/on-error-continue on the tool_call; timeout 3600s on wait_for_event),
  Reads/Writes chips on every step that has them. Interactions verified:
  clicking `notify_big` inside the condition's Then list selects that step;
  clicking the `steps.fetch.result` reads chip on `calc` jumps the panel to
  `fetch`; the Raw definition toggle reveals the JSON. The
  `qa84-explain-allkinds` playbook was left in place for phase-3 verification.

## Deviations and surprises

- **Version is 0.18.0, not 0.17.0 as PHASE.md said.** A concurrent session
  shipped plans/012 phase 1 (spec batching) as 0.17.0 (commit 252e5ad) while
  this phase was in flight, in the same working tree. This phase's commit was
  staged file-by-file to exclude that work; after 252e5ad landed the stamps
  were re-bumped to 0.18.0.
- QA Luna on 8766 loads plugins from its own managed dir
  (`.../87e8d157.../scratchpad/qa084-managed`), not `~/.luna/managed_plugins`
  — it was still running plugin-playbooks 0.14.0, whose backend predates the
  `code` kind. Synced the current tree there and restarted the server
  (relaunched with the same cwd/env, `env -u ANTHROPIC_API_KEY`).
- Backend validation rejects `steps.<agent_step_id>.result` when the step has
  an `output_schema` — outputs are the schema keys (hint: `_dry, _raw,
  summary`). The explain panel renders whatever the definition says, so no UI
  change needed; worth remembering when authoring fixtures.

## Reassessment of remaining phases

Phase 3 (per-kind fixture render tests completion, drift test against
`definition.py` fields via an exported stepdef_fields.json, Code-tab pblang
highlighting through the `Code` primitive, JSON-tree for run raw
inputs/outputs) is unchanged and still worth doing. Two notes:

- The drift test matters more now: phase 2 found `timeout` missing from
  types.ts — exactly the class of bug the phase-3 test automates away.
- Phase-2 render tests already cover all 12 kinds, so phase 3's "fixture
  tests completion" reduces to: richer fixtures (edge values: list `over`,
  fan_in variants, empty branches) plus the drift test.
