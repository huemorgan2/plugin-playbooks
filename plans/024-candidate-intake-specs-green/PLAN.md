# 024 — candidate-intake specs green + dry-run diagnostics

## Problem

The `candidate-intake` playbook (tenant vaselin-scanny-2, live v46) had all 20
specs red. Root cause was **spec debt** left by earlier definition refactors,
not a runner bug:

1. Every spec was missing a `pick_other` stub. `pick_other` is a code step;
   in a dry-run unstubbed code/tool steps return a simulated placeholder
   (`{_dry, _note}`). `dupe_branch` reads `steps.pick_other.result.found`, so
   every spec aborted at the first branch.
2. 13 specs still carried dead stub keys from before the refactor
   (`composio__monday__*`, `send_chat_message`) and an orphaned
   `compute_and_merge` (renamed to `compute_merge`).
3. Several `expect` blocks named steps that v46 removed (`link_to_job`,
   `notify_conflict`, `notify_merge`).

The failures were also **cryptic**: reading a field off an unstubbed
placeholder produced `... is a dict with keys: _dry, _note`, which never told
the author to add a stub.

## Scope

- Rewrite all 20 specs against the real v46 topology → 20/20 green.
- Framework improvements (plugin only, no Luna-core change):
  - Actionable dry-run diagnostic: name the unstubbed step and say to stub it.
  - `playbook_spec_add` tolerates a JSON-string `spec=`/`specs=` (agents
    routinely stringify object args).
- Ship 0.41.0, upgrade the tenant, push the corrected specs, verify live.

## Non-goals / deliberate calls

- **Did not** change dry-run to execute code steps. Code-not-executed is a
  documented design choice (022 P5); executing pure code in dry-run is a
  plausible future improvement but has prod blast radius across other
  playbooks and could not be validated locally without a real jail. Recorded
  as a follow-up rather than shipped hastily.
- Specs stay stub-based, so phone/merge specs assert routing + template flow
  (that the normalized/merged value reaches the mutation), not the code
  logic itself.
- No live/destructive Monday runs (the conflict branch emails a real
  recruiter and the merge branch deletes an item) — pure dry-run spec editing.
