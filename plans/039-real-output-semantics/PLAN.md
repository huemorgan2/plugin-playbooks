# Treat real playbook execution as execution evidence, not semantic proof

## Trigger and evidence

In the 17 September forced-condensation run, a delegated builder produced a runnable playbook and a real candidate run succeeded. The saved process emitted thirty valid-looking JSON files, but it substituted abbreviated IDs for source identifiers. The parent published it and later wrote an aggregate that disagreed with the files. A green runtime status was mistaken for a business-correct result.

## Change

In the delegated Python authoring brief, require comparing the real run's persisted output with the original input/specification for identifier and field-shape preservation before publishing. Make explicit that a successful run status proves execution only. Keep the existing dry-run and real-run distinction and the owner-request context from version 0.57.5. Bump the plugin version for this changed prompt.

## Verification

Test that the generated prompt contains the source/output comparison requirement without changing tool-result envelopes or legacy format behavior, run the full Playbooks suite, rebuild the fixed plugin artifact, and retest the exact long mission. Preserve the previous failing receipt and avoid assigning causality to the prompt change without a passing paired rerun.
