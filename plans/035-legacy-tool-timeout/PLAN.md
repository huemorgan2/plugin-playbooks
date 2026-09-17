# 035 — bound legacy tool steps without replaying uncertain effects

Status: owner-authorized harness repair, 2026-09-16. Existing `v2-runtime` checkout; bump plugin 0.57.1 to 0.57.2 while preserving its pre-existing `uv.lock` edits. This plan is for the still-executable legacy pblang runner; the v2 Python playbook path is unchanged.

## Problem

The legacy `tool_call` handler ignores a declared `step.timeout` and can hold a run indefinitely. The red reproduction `test_tool_step_timeout_is_enforced` records this. A timed-out external write may have committed, so a configured retry must not automatically replay it.

## Change and checks

Apply the declared bound to the live handler call. On timeout, record a truthful error that names the uncertain effect and bypasses automatic retries. Keep dry-run behavior and successful output shape intact. Run the targeted red reproduction, nearby legacy tests and the full plugin suite. Preserve the other three legacy red failures as explicit open work; do not manufacture passing waits by parking them without a real resume path.
