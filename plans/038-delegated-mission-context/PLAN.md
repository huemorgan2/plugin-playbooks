# Preserve mission context in delegated playbook work

## Evidence

The live `long.reconcile-single-directive` episode on 17 September 2026 authenticated successfully, completed one owner turn, and made eight tool calls, but passed only 1/33 assertions. The main agent called `playbook_agent` with the calculation rules while omitting the owner's exact `dojo-long/...` workspace path. The delegate received only that abbreviated `task`; its candidate used `input` and `output` relative to the file-store root, found no batch files, and stopped without a published playbook or any reconciled output. The complete event, delegation, and assertion receipts are in `dojoP/results/20260917-live-smoke/episode-00/`.

## Change

1. Carry the latest owner request from the active conversation into the delegate's brief through `ctx.conversations`, the sanctioned read-only SDK surface. Keep the explicit `task` authoritative for the requested edit, and present the owner message as bounded reference context so exact paths, constraints, and acceptance conditions survive handoff. Fail open when the reader is unavailable.
2. Update the small delegation skill to tell the main agent to copy exact file paths, workspace names, and requested final action into the work order. Keep its size guard and tool list intact.
3. Add a regression test in which the task omits the namespaced workspace but the owner message contains it; assert that the delegate sees that path and the no-publication constraint. Verify old fake contexts still work.
4. Bump the plugin manifest and package version to 0.57.5, run focused and full Playbooks tests, then run a fresh isolated live L01 episode with a new plugin overlay and compare artifact assertions. Do not alter the in-flight six-scenario candidate campaign.
5. The first full-suite run under simultaneous live missions exposed a SQLite in-memory fixture failure (`no such table: playbook_runs`) in a parked-run restart test; the isolated test passed. Make this fixture file-backed so a replaced connection sees the same tables, then rerun the affected test and full suite. This is a test-harness stabilization, not evidence of a product failure.

## Completion criteria

The child prompt includes the exact owner workspace and constraints even if the parent task omits them; no owner message body is written to the tool-result envelope or progress card. The revised plugin loads in the disposable candidate profile, and the live mission's result is reported with its actual assertions rather than inferred from the unit test.
