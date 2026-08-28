# Phase 3 — Guarantees + polish

## Scope

1. **Drift test against definition.py** (the phase's core deliverable — phase 2
   proved its worth when `timeout` turned out missing from types.ts):
   - A pytest exports the authoritative StepDef field list from
     `plugin_playbooks/definition.py` into
     `ui-src/src/playbooks/explain/stepdef_fields.json` (checked in; the test
     fails when the file is stale, so CI catches backend drift).
   - A vitest test asserts every exported field is either consumed by an
     explainer/dataflow/headline/footer (an explicit CONSUMED list mirrored in
     the test) or on an explicit IGNORED list with a reason. A new backend
     field breaks the test until the UI decides what to do with it.
2. **Richer fixtures** for existing render tests (phase 2 already covers all
   12 kinds): list-valued `over`, `fan_in` count variants, `count` loops,
   empty branches, `state` `delete` op, template-bearing `show` entries.
3. **Code-tab pblang highlighting**: the Code tab currently renders the pblang
   source as a plain `<pre>` — switch it to the `Code` primitive (Python
   grammar is close enough for pblang).
4. **JSON-tree for run raw inputs/outputs**: the exec section's raw JSON
   (`showRaw`) becomes a collapsible typed tree (keys bold, values through the
   same token colors) instead of a stringified blob. Zero-data rule applies.

## Deliverables

- `stepdef_fields.json` + generating pytest (`tests/test_stepdef_fields_export.py`
  or similar) + vitest drift test.
- Fixture additions to `explainers-kinds.test.tsx`.
- Code tab using `Code`; run raw data as a JSON tree component (in
  `explain/primitives.tsx` or a small new file).

## Verification

- `npm run build` clean; vitest green; full pytest green.
- Real-Luna check (QA 8766, CDP): Code tab highlighted; a run's raw
  inputs/outputs render as a tree (use qa84-hello which has runs, or run
  qa84-probes); step panel still correct on `qa84-explain-allkinds` (left in
  place by phase 2).

## Ship

Version 0.19.0 (all three stamps), commit, push, publish to marketplace.
Watch for concurrent sessions in this working tree (plans/012 shipped 0.17.0
mid-phase-2): re-check `git log`/stamps before bumping, stage file-by-file.
