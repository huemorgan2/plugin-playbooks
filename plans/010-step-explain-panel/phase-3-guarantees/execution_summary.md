# Phase 3 — execution summary

## What shipped

Version **0.19.0** (all three stamps: `plugin_playbooks/__init__.py` manifest,
`plugin_playbooks/luna-plugin.toml`, `pyproject.toml`).

- **Drift guard against definition.py** (the phase's core deliverable):
  - `tests/test_stepdef_fields_export.py` exports the authoritative StepDef
    field list (wire names, 39 fields) to
    `ui-src/src/playbooks/explain/stepdef_fields.json` (checked in); the test
    fails whenever the file is stale, `STEPDEF_FIELDS_WRITE=1` regenerates.
  - `ui-src/src/playbooks/explain/__tests__/stepdef-drift.test.ts` asserts
    every exported field is on an explicit CONSUMED list (all 39 are — the
    IGNORED list is empty today), with staleness and overlap checks. A new
    backend field now breaks CI until the explain UI handles it.
  - While building the CONSUMED list: `count?: number` removed from
    `ui-src/src/playbooks/types.ts` — it does not exist in the backend StepDef
    and nothing in ui-src used it (drift in the opposite direction).
- **Richer render fixtures** in `explainers-kinds.test.tsx` (+6 tests):
  literal-list `over`, while/until guards, fan_in count variant, absent
  fan_in + empty branch, `state` delete op, template-bearing `show` entry.
- **Code tab highlighting**: the Code tab's plain `<pre>` in
  `PlaybookEditor.tsx` replaced with the `Code` primitive (line numbers +
  python-ish token colors; pblang is close enough). The `code-view` testid is
  preserved on a wrapper div.
- **JSON tree for run raw data**: new
  `ui-src/src/playbooks/explain/jsontree.tsx` — collapsible typed tree
  (keys bold, scalar colors matching the definition token palette, per-node
  collapse with "N keys / N items" summaries, depth ≥ 2 starts collapsed).
  The exec section's "Show raw input / output" now renders Resolved inputs
  and Output through it instead of `JSON.stringify` blobs. New test file
  `jsontree.test.tsx` (4 tests) covers typed rendering, the zero-data rule,
  collapse/expand, and arrays.

## Verification

- `npm run build` clean (tsc + vite); vitest **100/100** (was 87);
  pytest **186 passed** (includes the new export test).
- **Real Luna** (QA on 8766, plugin tree synced into its managed dir
  `.../87e8d157.../scratchpad/qa084-managed`; the running server picked up the
  rebuilt static UI without a restart — asset hashes in the served index
  matched the fresh build). Verified via CDP (:9222):
  - Code tab on `qa84-explain-allkinds`: highlighted source, line numbers,
    590 colored token spans.
  - `qa84-hello` Runs tab → newest run → Show on canvas → step panel →
    Show raw input / output: both Resolved inputs and Output render as JSON
    trees with the real run payloads (nested `result` object included);
    collapsing the root shows `{ 1 key }`.
  - Explain-panel regression on `qa84-explain-allkinds`: headlines,
    DataFlow, and footer chips still correct across six sampled kinds
    (tool_call, agent_step, condition, loop, state, wait_for_event).

Commit: this commit. Published to marketplaces.com.ai as official 0.19.0.

## Deviations and surprises

- PHASE.md listed "`count` loops" as a fixture case (carried over from
  PLAN.md); the backend StepDef has no `count` field — the field was stale in
  types.ts and is now deleted rather than fixture-tested. Exactly the class
  of drift the new tests exist to catch.
- None otherwise; no concurrent-session commits landed during this phase
  (history re-checked before the bump: HEAD was still 19ec4dd).

## Reassessment of remaining phases

Phase 3 is the final phase of plan 010 — the plan is complete. The explain
surface now has: total per-kind coverage (phase 2), derived data flow +
footer chips (phase 2), and drift-proofing against backend changes plus
highlighted code/raw-data views (phase 3). No follow-up phases proposed;
future StepDef changes will announce themselves through the drift tests.
