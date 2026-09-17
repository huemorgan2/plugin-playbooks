# Plan 041 — wake the agent after a recovered candidate run

The dojoP high-budget crash trial killed the server while an `agent-candidate` test run was active. Playbooks resumed that v2 run on the new serving loop, and the run completed with exact saved artifacts. The initiating tool call died with the old server, however: the recovered test run had `wake_on_complete=false`, and `RunCompletionWake` discards all test-run events. No agent was told to verify the saved results and publish the candidate; the Tasks watchdog only intervenes after its stall interval.

1. At serving-loop restart, mark only journaled, interrupted `agent-candidate` test runs with an originating conversation for a durable completion wake, before spawning their resumed run tasks. Ordinary synchronous candidate tests retain their inline result and do not wake twice.
2. Allow the wake service to deliver those explicitly marked candidate-test completions, while continuing to suppress unmarked tests and nested subtask runs.
3. Tell the awakened agent that the recovered run is candidate evidence, that it must verify the persisted effects against the source contract, and that publication still requires the normal gate and owner approval. Use a finite long-task allowance for that continuation.
4. Test the restart stamp and wake routing, run the full Playbooks regression and Luna companion tests, then run a fresh real-model dojoP crash episode with the same independent source and publication oracle. Preserve the earlier raw receipts.

The fix succeeds only when the fresh trial reaches a published reusable process and all exact artifacts after a real kill/restart, without replaying the owner assignment.
