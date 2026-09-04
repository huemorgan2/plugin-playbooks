# 025 — execution summary: spec-repair agent eval

## What was done

Took the now-green `candidate-intake` suite (20 specs, tenant vaselin-scanny-2,
live v46) and deliberately broke **10 of the 20 specs**, one distinct failure
category each. Then handed a **fresh conversation** to the live tenant agent with
a neutral prompt (`fixtask.txt`): "the spec suite is failing, investigate and fix
the SPECS, one at a time, explain each, no live Monday, report the pass count."
No answers, no hints, no per-spec guidance. Measured the result independently via
`POST /playbooks/candidate-intake/specs/run?version=46` (not the agent's prose).

Baseline after storing the breakages: **10 pass / 10 fail** (the other 10 specs
were left untouched and stayed green — a control).

## Headline result

**Agent fixed 9 of 10 → 19/20 green.** It correctly diagnosed all 10 in its
narration, restored 7 exactly, fixed 2 more with acceptable variations, and left
**1 red** (`missing-job-id-empty`) by over-engineering — it trusted the broken
value as the spec's intent and tried to build a real failure path instead of
reverting it.

## Per-category results

| # | spec | category (the fuckup) | fixed? | how the agent fixed it |
|---|------|-----------------------|--------|------------------------|
| 1 | new-candidate-unique | missing-required-stub (deleted `pick_other` stub) | ✅ exact | re-added the `pick_other` stub |
| 2 | happy-path-no-duplicate | stub-key-typo (`find_dupes`→`find_dupez`) | ✅ exact | renamed the stub key back to `find_dupes` |
| 3 | phone-normalization-972 | wrong-expected-value (`0501234567`→`9999999999`) | ✅ variation | restored the correct value; also relocated the assertion (minor semantic drift, still green) |
| 4 | duplicate-exact-name-merge | steps_ran-ordering (`old_cols` moved after `merge_fields`) | ✅ exact | restored `old_cols` before `compute_merge`/`merge_fields` |
| 5 | subitem-already-exists | nonexistent-step-ref (added `link_to_job`) | ✅ exact | removed `link_to_job` from `steps_ran` |
| 6 | **missing-job-id-empty** | **wrong-status (`done`→`failed`)** | ❌ **red** | misread the broken `failed` as intent; tried to engineer a real missing-job-id error path, couldn't, left it red |
| 7 | phone-strip-spaces-dashes-parens | wrong-stub-shape (`get_cols` = bare list) | ✅ exact | re-wrapped the list under `column_values` |
| 8 | duplicate-different-name-conflict | branch-routing-mismatch (`pick_other.name`='Alice') | ✅ variation | restored a differing old name so `name_check` routes to the conflict branch; also strengthened the test with a `send_conflict_email` tool_calls assertion |
| 9 | merge-fills-empty-fields-and-runs-full-pipeline | wrong-tool_calls (added `send_email` count=1) | ✅ exact | removed the bogus `send_email` tool_calls expectation |
| 10 | phone-monday-object-format | expects-nonexistent-error (added `error_contains`) | ✅ exact | removed the `error_contains` on a `done` run |

The one failure (#6) is the most telling. A spec named `missing-job-id-empty`
with `expect.status: failed` reads as "this run is supposed to fail" — so the
agent chased the failure path (`get_job IS running when it shouldn't`) rather than
taking the trivial fix of `failed → done`. It ran out of turn mid-investigation.
The suite-name + a plausible-but-wrong assertion actively pointed it the wrong way.

## Issues found

1. **Plugin bug — false "was not stubbed" diagnostic (FIXED, shipped 0.42.0).**
   On #7 the run failed with `'list object' has no attribute 'column_values'`
   correctly, but the 0.41.0 dry-stub hint appended *"Step(s) [get_cols] were not
   stubbed…"* — **false**: `get_cols` WAS stubbed (with a wrong-shape list). Root
   cause: a stubbed tool/code step's recorded wrapper carries top-level
   `_dry: True` (the run is still a dry run), and `_is_dry_placeholder` matched
   that, treating every stubbed step as an unstubbed placeholder. It only surfaces
   when a template fails while reading a stubbed-but-wrong-shape step. Fixed by
   also requiring `stubbed is not True`; 3 regression tests added
   (`test_dry_stub_diagnostic.py`). A wrong-shape stub now fails with the real
   attribute error and no misleading stub advice.

2. **Agent over-engineers trivial breakages / reinterprets intent from the spec
   name + broken value.** #6 needed a one-token revert; the agent instead treated
   the broken `failed` and the suite name as authoritative and tried to construct
   a real error, leaving the suite red. Behavioral, not a code bug — but it argues
   for spec assertions the agent can't mistake for product intent, and for a
   diagnostic that says "the run completed `done`; your `expect.status: failed`
   does not match" prominently.

3. **Agent's drive to "make it pass" overrides "store verbatim"** (found while
   seeding the fixtures). Told to store intentionally-broken specs verbatim, the
   transport agent silently *corrected* one (re-added the `pick_other` stub → all
   20 passed). Only an explicit "these are INTENTIONALLY BROKEN QA fixtures, store
   EXACTLY, do NOT correct" framing stopped it.

4. **Heavy tool churn.** ~20 `playbook_spec_add` calls plus load_skill/load_tools
   hops and repeated read/read_with_query on the definition to fix 10 specs —
   functional but token-expensive.

## Verification

- Plugin unit suite: **361 passed** (was 359; +2 new dry-stub tests, +1 within an
  existing test function).
- Published **plugin-playbooks 0.42.0** to marketplaces.com.ai/official
  (`latest_version` = 0.42.0).
- Live tenant upgraded 0.41.0 → **0.42.0** (enabled, no error, 29 tools).
- Live `candidate-intake` suite re-run on 0.42.0: **20 total, 20 passed, 0 failed**
  (tenant restored green; the fix does not regress the suite).

## Non-goals honored

No live/destructive Monday runs — the conflict branch emails a real recruiter and
the merge branch deletes an item. Entire eval stayed in non-destructive dry-run
spec editing. Did not change agent-behavioral findings into code changes beyond
the diagnostic bug (issues 2–4 recorded for a future prompt/UX pass).
