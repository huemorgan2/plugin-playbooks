# 025 — spec-repair agent eval

## Goal

Deliberately break the (now-green) `candidate-intake` spec suite in 10 distinct
ways, then ask the live tenant agent to fix the failing specs with no hints, and
measure how it does — per-category success/failure — while surfacing issues in
the plugin's diagnostics and the agent's behaviour.

Non-destructive: spec editing only. No live Monday runs (the conflict branch
emails a real recruiter; the merge branch deletes an item).

## Fixtures

10 breakage categories, one per spec (see `manifest.json` for the exact fuckup +
the intended correct fix, and `broken10.json` for the stored spec bodies):

| # | spec | category |
|---|------|----------|
| 1 | new-candidate-unique | missing-required-stub |
| 2 | happy-path-no-duplicate | stub-key-typo |
| 3 | phone-normalization-972 | wrong-expected-value |
| 4 | duplicate-exact-name-merge | steps_ran-ordering |
| 5 | subitem-already-exists | nonexistent-step-ref |
| 6 | missing-job-id-empty | wrong-status |
| 7 | phone-strip-spaces-dashes-parens | wrong-stub-shape |
| 8 | duplicate-different-name-conflict | branch-routing-mismatch |
| 9 | merge-fills-empty-fields-and-runs-full-pipeline | wrong-tool_calls |
| 10 | phone-monday-object-format | expects-nonexistent-error |

Validated locally against the v46 definition: exactly these 10 go red, the other
10 stay green (`live_baseline_fails.json` = the live baseline diagnostics).

## Method

1. Store the 10 broken specs on live v46 verbatim (transport turns).
2. Confirm 10 pass / 10 fail via `specs/run?version=46`.
3. Fresh conversation, neutral prompt (`fixtask.txt`): "tests are failing, fix
   the specs, one at a time, explain each; no live Monday." No answers given.
4. Re-run the suite; diff the agent's stored specs against the original green +
   the manifest's intended fix. Record per-category outcome + issues.

Results in `execution_summary.md`.
