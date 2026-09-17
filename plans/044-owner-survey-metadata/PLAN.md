# Declare Playbooks owner-turn guards in plugin metadata

## Problem

The merged Luna survey guard named `playbook_propose` directly in core, violating the plugin boundary and failing the architecture invariant. It also needed to hold Playbooks writes while answering a question about what is stored.

## Change

1. Add optional `survey_before_create` and `store_write` fields to the SDK `ToolDef` exposed by Luna core.
2. Declare `playbook_list` as the required survey for `playbook_propose`; declare propose, edit and publish as store writes.
3. Have Luna's turn wrappers build their guards from registered plugin definitions. A successful empty list counts as a completed survey, while unrelated reads and errors do not.
4. Bump Playbooks to 0.57.11 in the package, runtime manifest and data manifest; run the Playbooks suite and Luna integration tests together with the default plugin set.
5. The full regression exposed a 100 ms timer race in the restart deadline test. Persist an elapsed deadline after stopping the old process, so the test verifies recovery from downtime deterministically.

## Limit

These flags guard owner and store-question turns. They do not change Playbooks' candidate validation, publication approval or scheduled execution.

## Verification

The full Playbooks suite passed: 779 tests. Luna's full offline core regression with this plugin overlaid passed: 2,937 tests, 41 skipped. The restart deadline case was tested by persisting an elapsed deadline after stopping the old runner, then reconciling with a fresh runner.
