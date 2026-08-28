# Phase 4 — dojo end-to-end (fix task, real browser, baseline comparison)

## What phases 1–3 left for this phase

The mechanics are all proven (delegation core, live card, approval
parking). What has NOT been shown is the plan's headline scenario: a
*fix* task on a broken playbook, where the delegate must diagnose before
editing — Understand → Change → Prove → Ship all doing real work — plus
the cost comparison against doing the same job in the main context.

## Scope

No plugin code changes expected (any bug found gets fixed + tested, as in
earlier phases). This phase is scenario construction + measurement.

1. **Fixture**: build a `candidate-intake` playbook on QA Luna (dojo
   turns): an `update_phone` code step that normalizes phone numbers +
   specs, promoted green at v1. Then break it — edit the code so the
   phone format comes out wrong (e.g. stops stripping punctuation),
   leaving at least one spec failing on the candidate.
2. **Baseline run** (fresh conversation): "fix the phone format in
   candidate-intake and make it live — do the work yourself in this
   chat, don't hand it to the background agent." Record wall-clock and
   the done event's `context_input_tokens` for the whole fix.
3. **Delegated run** (fresh conversation): "fix the phone format in
   candidate-intake" — the natural phrasing; Luna should load the
   delegation skill and hand off. In the open browser: the card posts,
   phases light in order with real step counts, promote parks →
   approve from the chat card → done. Screenshots: streaming mid-run,
   parked, done. Record the same two numbers for the main context.
4. **Comparison**: wall-clock and main-context tokens, baseline vs
   delegated, written into the execution summary. The claim under test:
   the delegated main turn stays small (~1KB payloads, card carries the
   detail) while the baseline drags the whole edit/spec loop through the
   owner's context.

## Verification

- Both runs end with candidate-intake live and specs green (delegated run
  proven in the browser; baseline via API/screenshot).
- Card behavior consistent with phases 2–3 (live updates without reload,
  parked banner, terminal state, no console errors).
- Numbers recorded for both runs; summary written; no regression in the
  full pytest suite (nothing should have changed).
