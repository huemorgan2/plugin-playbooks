# 036 — fail closed for legacy waits and preserve unknown orphan outcomes

Status: owner-authorized follow-up, 2026-09-16, existing `v2-runtime` checkout. Bump 0.57.2 to 0.57.3 without discarding the pre-existing lockfile work.

## Problem

The old pblang executor still accepts `wait_for_approval` and `wait_for_event` but immediately returns fake success. A restart while its tool step is in flight stamps the run `failed`, even though the external effect may have committed. New Python playbooks have durable approval/event parking and a distinct `timed_out_unknown` outcome; the legacy executor has no journal from which it can safely replay arbitrary steps.

## Safe compatibility behavior

- Reject a legacy definition containing either wait kind **before its first step**, including waits nested in conditions, loops or parallel branches. Record a typed run failure telling the owner to migrate it to the Python runtime. Never auto-approve or claim an event was received. Leave the v2 park service and its working wake paths untouched.
- For an orphaned legacy run with a running step, record `timed_out_unknown` / `OutcomeUnknown` and identify the step. Do not replay the effect or run later steps. Keep the existing failed-orphan behavior for a row with no in-flight step. Deliver any promised completion wake with the truthful status.
- Strengthen the three legacy reproductions to assert these safety outcomes and no downstream effect. This is a fail-closed compatibility repair, not an implementation of live v1 waits or automatic v1 resume.

## Verification

Run the focused reproductions, v1 runner and v2 park/resume tests, then the complete Playbooks suite and the fixed candidate's dojoP contracts. Keep a migration follow-up for any owner with stored legacy wait definitions; do not claim that the old language acquired durable waiting.
