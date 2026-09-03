# 024 — execution summary

Shipped as **0.41.0**.

## What changed

Three pieces, each with tests.

### 1. candidate-intake specs rewritten → 20/20 green

All 20 specs were red on live v46. Root cause was spec debt, not a runner
bug (see PLAN.md):

- Added the missing `pick_other` stub and a `find_dupes` stub to every spec.
  Both feed a branch condition via strict attribute access
  (`steps.pick_other.result.found`, `find_dupes.result.dupes.entries`), so an
  unstubbed dry-run placeholder aborted the run at the first branch.
- Removed dead stub keys left by earlier refactors (`composio__monday__*`,
  `send_chat_message`, orphaned `compute_and_merge`).
- Dropped `expect` references to steps v46 no longer has (`link_to_job`,
  `notify_conflict`, `notify_merge`).
- Removed condition wrappers (`dupe_branch`, `name_check`, `old_sub_guard`)
  from ordered `steps_ran` lists — condition steps record their trace entry
  after their subtree, so they can't precede their children.
- Phone specs assert the needle `countryShortName` (bare), not
  `"countryShortName": "IL"` — `output_contains` matches against
  `json.dumps(step_output)`, where nested JSON-string quotes are escaped.

Specs stay stub-based: phone/merge specs assert routing + template flow (the
normalized/merged value reaching the mutation), not the code logic itself.
Verified locally via `_local/localrun.py` against the v46 definition → 20/20.

### 2. Actionable dry-run stub diagnostic (runner.py)

Reading a field off an unstubbed placeholder previously produced
`... is a dict with keys: _dry, _note`, which never told the author what to
do. Now `_undefined_ref_detail`, `_render_template`, and strict
`_eval_expression` failures detect the `{_dry: true}` placeholder and append:
"Step(s) [X] were not stubbed and returned a simulated dry-run placeholder —
add a `stubs` entry for each so its output is defined."

Tests: `tests/test_dry_stub_diagnostic.py` (4 tests) + updated
`test_undefined_ref_error_names_real_keys` in test_specs.py.

### 3. playbook_spec_add tolerates stringified args (agent_tools.py)

Agents routinely pass object arguments as JSON strings. `_spec_add` now
coerces a string `spec=`/`specs=` via `json.loads`, returning a clear error
if the string isn't valid JSON. This directly unblocked the live push, where
the agent had stringified the `specs` object.

Test: `test_spec_add_accepts_stringified_specs` in test_specs.py.

## Verification

- Full suite: 359 passed.
- Local dry-run of all 20 specs against v46: 20/20.
- Live tenant (vaselin-scanny-2): pending upgrade to 0.41.0 + spec push +
  run — recorded below once confirmed.

## Non-goals

Did not change dry-run to execute code steps (documented 022 P5 design
choice; prod blast radius, not validatable locally). No live/destructive
Monday runs — the conflict branch emails a real recruiter and the merge
branch deletes an item, so all work stayed in non-destructive dry-run spec
editing.
