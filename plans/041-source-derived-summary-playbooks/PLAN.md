# Make summary artifacts part of the saved process

## Evidence

The merged 16-turn reconciliation episode produced all thirty exact batch files, but its `output/manifest.json` failed the required schema and totals check. The owner asked Luna to establish a reusable process. The agent later transcribed totals outside that process and described the manifest as verified. The separate Playbooks delegate improvement fixed a one-turn version of this job, but the ordinary `playbook-authoring-v2` skill still omitted source-derived summary guidance.

## Change

Add a short rule to the ordinary v2 authoring skill: calculate multi-file totals from actual source records inside the saved playbook, reopen the persisted summary after a real run, and check its exact keys, counts and arithmetic before a correctness claim. Preserve the existing candidate/publish and approval rules. Keep the skill below its 6,144-byte limit and bump the immutable plugin version in package and both manifests.

## Validation

Run the skill and full plugin suites. Retest the 16-turn reconciliation mission on merged Luna with the full default-plus-DB-and-Playbooks profile. Score the saved manifest and completed owner turns, and compare with the frozen merged baseline; a prompt change alone is not proof of improvement.
