# 037 — stop legacy runs on an uncertain tool timeout

Status: owner-authorized reliability follow-up, 2026-09-16.

## Starting evidence

Version 0.57.3 bounds `tool_call` steps and suppresses their configured retries. However `_execute_step` still applies `on_error: continue` after `ToolStepTimeout`, allowing downstream effects, and records the whole run as a definite `failed` outcome even when the external tool may have committed. The full 765-test suite had no `on_error` timeout case.

## Change

1. Add RED cases for a timed-out tool with `on_error: continue` and an immediately following effect, and for the default abort outcome. Include nested condition, parallel and subtask containers, which can otherwise swallow a child's uncertain result. All must end as `timed_out_unknown` / `OutcomeUnknown`, identify the step, and never run downstream work or retry the uncertain call.
2. Treat `ToolStepTimeout` before the generic `on_error` policy. Complete its step as `timed_out_unknown`; complete the run with the same status and typed error. Leave ordinary deterministic tool failures and v2 execution unchanged.
3. Raise the package version to 0.57.4 and update the disposable candidate overlay. Run focused, complete Playbooks, Luna integration and dojoP fixed-profile checks.

The owner must inspect external state before manually rerunning a timed-out effect; this change does not claim exactly-once execution.
