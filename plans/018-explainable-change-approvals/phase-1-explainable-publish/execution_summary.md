# Phase 1 — execution summary

## What shipped

`plugin_playbooks/agent_tools.py`:

- `playbook_publish` and `playbook_rollback` gained a required `explanation`
  argument. The handler (not the schema) refuses anything missing or under
  80 characters with a steering hint (`_EXPLANATION_HINT`) telling the agent
  to write 2–6 owner-language sentences and call again. Nothing is touched
  before the gate.
- Both tools flipped `prompt_always` → `auto_approve`; the handler raises
  the owner approval itself. `_do_publish` was restructured into three
  parts: session A runs the five gates under the row lock and gathers the
  card material (live vs target pblang code via `_derive_code` /
  `_version_code`, manifests, spec-name sets per version), commits the
  spec/probe caches, and releases the lock; the owner decision waits with
  no transaction open; session B re-locks, re-checks the approved target is
  still current (candidate pointer unchanged / version still not live), and
  flips live exactly as before.
- New closure `_request_publish_decision`: builds ONE
  `ctx.approval.request(kind="playbook_change", ...)` with
  `payload={"name", "version", "action"}` (identity only) and a luna-094
  `presentation` — eyebrow "Playbook change", headline = first explanation
  line clipped to 90 chars, explanation = agent text + one evidence line,
  `changes[]` = code diff, manifest diff (restores only — candidate publish
  never changes the manifest), spec-set change note. Rejection returns a
  refusal carrying `owner_reason` and a stand-down hint. `ctx.approval`
  is accessed under try/except (it raises RuntimeError when unwired); no
  ctx / no engine → publish proceeds ungated with a warning log, matching
  the old reality that `prompt_always` also didn't prompt in those contexts.
- `publish_autonomy="auto"` is honored for the first time: in the ops
  chat's `fix_publish` state it records `record_auto_approval(...)` (same
  presentation, so History reads identically) and proceeds without asking.

Tests:

- New `tests/test_explainable_publish.py` (6 tests): steering refusal on
  missing/short explanation with no side effects; approved path files
  exactly one rich approval (payload identity, presentation shape, code
  diff before/after) then flips; rejected path leaves live and candidate
  untouched and returns the owner's reason; candidate edited during the
  approval wait → flip refused; `publish_autonomy="auto"` +
  `fix_publish` → audit-only, no blocking ask; rollback carries the
  reverse code diff.
- Existing suites updated: `evidence.py` exports a shared `EXPLANATION`
  constant; every `playbook_publish`/`playbook_rollback` handler call in 7
  test files passes it. `test_candidate_flow.test_new_tool_policies` now
  asserts `auto_approve` + required `explanation` for publish/rollback
  (run_candidate stays `prompt_always`). `test_waiting`'s drift test pins
  `_GATED_TOOLS` = prompt_always ToolDefs ∪ {publish, rollback} — the two
  still park the delegate (the handler blocks on the owner), they just are
  not core-gated anymore.

## Verification

Full suite: **313 passed** (baseline 307 + 6 new), zero failures.
Live-browser verification deferred to the combined E2E (master plan phase
4), per PHASE.md.

## Deviations from PHASE.md

- No jargon detector on the explanation — length + steering hint only; the
  ops prompt sections (phase 3) own the style pressure. A heuristic jargon
  gate would be guessy and fight the vocabulary-fix principle.
- Manifest diff ships for restores only: a candidate publish keeps the live
  manifest by design (`restore_manifest=not is_candidate`), so there is
  nothing to diff on that path.

## Surprises / learnings

- `_GATED_TOOLS` (delegation parking detection) is defined by "parks on an
  approval card", not by policy — moving to handler-side approvals must NOT
  remove tools from that set.
- `publish_autonomy` had been documented in the model since 0.26.0 but was
  consulted nowhere; handler-side approval finally gave it a place to act.

## Reassessment of remaining phases

No changes. Phase 2 (fix-proposal presentation + the two bugs) proceeds as
planned; the wake-turn `tools="all"` fix pairs with luna 0.91's
kind/state-carrying moment turns, which shipped today. Phase 3 unchanged;
0.30.0 stamps land there.
