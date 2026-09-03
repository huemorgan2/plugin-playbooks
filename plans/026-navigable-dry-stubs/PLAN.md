# 026 — Navigable dry stubs

Origin: luna repo plan 102 phase 4 (luna-code-findings.md #4). Verified in the
deployed context capture: any dry run whose later step references
`{{ steps.a.result.<field> }}` on an UNSTUBBED tool/code step dies with
StrictUndefined `UndefinedError` — the agent cannot smoke-test a playbook
without hand-writing stubs for every step first. The diagnostics (plans/024)
made the failure explain itself; this plan makes the failure unnecessary.

## Fix

`_DryStub(dict)` — the dry result placeholder becomes a navigable empty
Mapping instead of `{"_dry": True, "_note": ...}`:

- `__missing__(key)` returns a chained `_DryStub("<path>.<key>")`, so any
  depth of field access resolves instead of raising. Special cases: `_dry` →
  `True`, `_note` → the simulation note (self-description preserved for
  templates and existing assertions) — WITHOUT being real keys, so the stub
  stays empty.
- `__str__`/`__repr__` → `"<dry:<path>>"` — rendered values are visibly fake
  (plans/022 truthful-evidence policy holds).
- Seeded EMPTY: `{% if %}` sees falsy; `loop over=` a dry value iterates zero
  times (`_run_loop` maps a `_DryStub` to `[]` before the scalar-wrap).
- The wrapper stays a plain dict with real `_dry: True` and gains a real
  top-level `_note`, so transcripts/JSON keep the simulation marker
  (`_DryStub` itself serializes as `{}`).

Stubbed steps are untouched — a spec stub still scripts the result verbatim.

## Behavior changes (pinned tests updated)

- tests/test_specs.py `test_undefined_ref_error_names_real_keys`: loop over an
  unstubbed dry path no longer fails — rewritten to pin zero iterations +
  status done (missing STEP ids stay loud; that path pins the hint now).
- tests/test_plan004_code_and_functions.py: unstubbed code result equality
  updated (result is an empty navigable stub; `_note` via access, wrapper
  carries the marker).

## Test

tests/test_plan026_navigable_dry_stubs.py — TDD red first: unstubbed field
ref doesn't fail the dry run; chained access renders `<dry:...>`; loop over
unstubbed value iterates 0 times; stubs still verbatim; self-description
retained; code steps behave the same.
