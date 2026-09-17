# Finish or disclose a resumed candidate wake

## Finding

A real eighteen-plugin crash trial resumed a named candidate run after restart. Its run finished successfully and the completion moment reached the original conversation, but the contained agent reaction aborted without a reply. Playbooks logged the moment as delivered and left the candidate unpublished. This is not a completed owner mission.

## Change

- Inspect the core muted-message result. A queued moment is owned by the active turn; a replied moment is complete. A silent or contained-aborted successful candidate wake gets one bounded, explicit follow-up after checking that its exact version is still unpublished.
- The follow-up asks for persisted-output verification and the ordinary gated publish flow, without rerunning completed batch work. If it also cannot respond, leave an owner-visible awareness note saying the candidate is ready but the live process remains unverified.
- Preserve candidate/live distinction and the exact approval gate. Never publish merely because a test run returned `done`.

## UX check

Read `vision/ux_guidelines.md`. The user-visible notification leads with the outcome (“Process not live”), uses one short support line, and avoids internal error labels. The detailed failed reaction stays in logs.

## Verification

- Add unit tests for one retry after an aborted candidate moment, no retry for queued or answered moments, and an honest note after a second non-response.
- The wake-stamp regression fixture must give concurrent background and caller sessions separate SQLite connections; its former shared in-memory connection lost a committed wake flag under full-suite load.
- Run the full Playbooks suite, Luna combined-plugin regression, and a fresh real-kill dojoP trial with DB, Files, Tasks, Playbooks and all default plugins loaded together.

## Outcome

Focused wake suite: 14 passed; the formerly racy wake-stamp check passed ten isolated repetitions. Full Playbooks suite: 778 passed, 8 warnings (`/private/tmp/playbooks-05710-full-final-20260917.log`). The U real-kill combined-plugin trial passed 65/65 within 199.552 active seconds, including the separate recovery deadline. Its exact publish approval was committed through the existing deterministic handler; the new retry branch was not exercised because the candidate wake aborted only after the playbook was already live. The retry and owner-disclosure branches are covered by focused tests. The final Luna full regression with this exact package source passed 2,834 tests, 41 skipped, 11 warnings (`/private/tmp/luna-full-054-05710-finalsource-20260917.log`).
