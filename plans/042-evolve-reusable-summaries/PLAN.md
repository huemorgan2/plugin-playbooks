# Keep later aggregate requirements in the reusable playbook

## Failure

The full 16-turn reconciliation retest on Playbooks 0.57.14 completed every owner turn and all 30 batch outputs, but the final manifest still had incorrect totals (64/65 checks). The agent tried the jailed calculator twice; the fixture denied it. It then wrote the aggregate outside the reusable playbook. The new v2 authoring-skill guidance was not seen: Luna loaded the small delegation skill and delegated initial playbook creation, then handled the later manifest request inline.

## Change

Place a concise reusable-summary rule in Playbooks' always-visible prompt section. A later multi-file aggregate request should trigger an edit to the existing saved playbook, using a delegate when appropriate, then a real run and inspection of the persisted summary. A hand-transcribed one-off file is not completion of the reusable-job contract. Keep the separate v2 authoring rule for agents that build inline. Bump both plugin manifests for a new immutable version.

## Validation

Check prompt registration and run the complete plugin suite. Retest the unchanged 16-turn scenario with merged Luna and the full 18-plugin profile. Require exact saved manifest, all 30 batch files and completed owner turns; a different prompt alone does not establish improvement.
