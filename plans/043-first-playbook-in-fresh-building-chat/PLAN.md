# Make the first repeatable job a saved playbook

## Failure

In the merged-main LONG crash trial, Luna planned to process 30 batches manually and put the requested named playbook last. The source owner request explicitly asked for a recurring saved process. In a fresh building conversation the Playbooks prompt section returned early when no playbooks existed, so its reusable-work guidance was absent at the decision point. The recovery replay similarly produced all 30 batch files before creating a playbook and then ran out of its bounded autonomous token budget.

## Change

Show a short building-chat section even when the playbook catalog is empty. For a recurring job, instruct Luna to create and test the named saved process before processing the entire workload manually, use a small sample for validation, then run and verify persisted outputs and live state. Do not add this rule to planning or Ops conversations. Keep all approval and publication gates intact.

## Validation

Test the fresh-building and planning prompt variants, run the full Playbooks regression, then retest the real 18-plugin LONG workload with Luna and DB/Files/Scheduler/Playbooks attached. Require exact artifacts and the named live playbook; prompt presence by itself is not success.
