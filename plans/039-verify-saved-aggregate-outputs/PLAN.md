# Require source-backed proof for saved aggregate outputs

## Failure

In the 2026-09-18 merged dojoP `long.reconcile-single-directive` trial, the delegated Playbooks builder processed all thirty batches correctly but left `output/manifest.json` as a zero-filled placeholder (63/64 checks). Its candidate run was green, it read the manifest, then reported exact nonzero totals and said the output matched the specification. Publication was still awaiting an owner approval. This shows that a green candidate run plus a file read did not cause the delegate to compare the saved artifact with the owner contract.

## Change and validation

1. Strengthen the Playbooks delegate's quality bar and publish checklist: numeric aggregates must be computed from the actual saved source records in the playbook; after a real candidate run, inspect the full persisted output for exact required keys, count and arithmetic invariants. A placeholder or unverified output blocks a success claim or publication.
2. Apply the rule to both python and pblang authoring, preserve existing approval and publication gates, and bump the plugin version in the manifest and TOML as required for immutable plugin releases.
3. Run the plugin's prompt and full unit suites. Retest the direct long mission with Luna and the complete default-plus-DB-and-Playbooks profile, comparing persisted file evidence and approval state. Preserve the original failed raw receipt.
