# Execution summary — unknown timeout stops legacy flow

The 0.57.4 legacy executor now treats a timed-out tool call as an unknown external outcome. It records the step and run as `timed_out_unknown` with `OutcomeUnknown`, then stops without applying retries or `on_error: continue`. The stop propagates through condition, parallel and subtask containers; an uncertain child run cannot let the parent execute a later effect. Ordinary deterministic errors retain their existing `on_error` behavior, and the v2 Python runtime is unchanged.

The new reproductions were RED: a direct `continue` path reached a later tool and marked the run done, and a child timeout likewise let its parent run a later tool. Focused tests now pass (**21** across the safety reproduction and async-run files). The complete suite passed **769 tests** with five warnings in 75.44 s (`research/agent harness check/evidence/fix-20260916/playbooks-0574-full-20260916.log`). `uv lock --check --offline` and `git diff --check` passed. The exact package was loaded in the final 18-plugin candidate (`dojoP/results/20260916-fixed-candidate-boot-final5.json`).

The status means external state requires inspection before manual retry; it does not guarantee that cancellation reversed a remote effect or provide automatic exactly-once recovery for old pblang runs.
