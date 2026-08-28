# 004 — execution summary

Shipped 2026-08-28. Two features from research/playbook-functions/idea.md:
`code()` steps (jailed Python, delegated to plugin-inline-code-run) and
`def` functions in pblang (inline macro expansion). plugin-inline-code-run
0.3.0 and plugin-playbooks 0.16.0 published to marketplaces.com.ai.

## Phase 1 — plugin-inline-code-run 0.3.0: JSON run mode (commit 2c8f03c)

- `json_mode.py`: wraps user code as the body of `__pb_main__(inputs)` so
  `return` works; stages `input_json` as `inputs/input.json`; reads the
  returned value from `outputs/result.json` (written with `default=str`).
  Results ride a file, never stdout — the jail discards stdout on timeout.
- `tool.py`: new optional `input_json` param on `code_run`. When present the
  wrapped code runs and the payload gains `result` (or `result_error` with a
  clear message when the code returned nothing / non-JSON). Legacy calls
  byte-identical behavior.
- 9 new tests (`test_plan003_json_mode.py`), incl. a real jailed round-trip.
  Suite: 130 passed, 1 skipped.

## Phase 2 — playbooks `code()` step kind

- `definition.py`: `StepKind.CODE`, `StepDef.source` + `StepDef.code_inputs`
  (named `source`, not `code` — the authoritative pblang code column exists).
- `compiler.py` `_build_code`: first positional must be a string literal;
  body syntax is pre-checked at compile time by parsing the same
  `__pb_main__` wrap the jail uses (errors point at the body line);
  `inputs=` must be a dict literal (values may be Jinja/expressions).
- `runner.py` `_run_code`: renders `code_inputs`, calls the `code_run`
  tool via the TOOL REGISTRY (`self._tools.get("code_run")`) — deliberately
  not a Python import (managed-loader module names make imports fragile).
  Missing tool → "code steps need plugin-inline-code-run". Failure surfaces
  the stderr tail; `result_error` fails the step. Output: `{result, stdout}`.
  Dry runs stub by step id and never touch the jail.
- `validation.py`: warning (degrade-visible) when `code_run` is absent;
  `code_inputs` values template-checked; source body deliberately NOT
  Jinja-checked (Python braces false-positive).
- `probes.py` `collect_tools`: code steps advertise `code_run`, so preflight
  probes it and promote refuses when the plugin is missing.

## Phase 3 — functions via macro expansion

All in `pblang/compiler.py`:

- The module statement loop moved to `_Compiler.compile_stmt_list(body,
  top_level=...)`, shared by module level and def bodies. New statement
  errors: `return` ("functions are procedures"), nested def, assigned
  function call ("functions have no return value").
- `register_function`: top-level `def` collected (define before first call);
  rejects reserved names, decorators, non-plain params, duplicates.
- `expand_call`: binds args in the CALLER's scope (each param becomes a
  `state` set step), compiles a deep copy of the body in a sub-compiler
  seeded with the outer scope (outer steps/vars readable; shadowing errors
  loudly), then renames everything the body defined with a per-call prefix
  (`notify__x`, second call `notify_2__x`) — ids/vars via key rename,
  references via a `\b(steps|vars)\.(name)\b` regex over all string values
  (compiled and raw Jinja look identical at that point). `source` values
  are excluded from the rewrite (jail Python is opaque). Recursion is a
  compile error; expansion depth capped at 8. Sub-compiler issues surface
  prefixed `in <fn>(): ...`.
- Call sites: top-level statements and nested step lists (`then`/`else_`/
  `body`/branches) both splice expansions.
- codegen: expanded IR round-trips (`compile(generate(ir)) == ir`) — the
  generated code contains the flattened steps, not the def (by design:
  the def is authoring-side sugar; candidates store the flattened truth).

## Phase 4 — reference, skill, UI, tests, ship

- `reference.py` cheatsheet: CODE STEPS + FUNCTIONS sections, code() in the
  combinator list, `steps.<id>.result` reference shape, "no def" intro fixed.
- Authoring skill (`__init__.py`): code() combinator bullet, CODE STEPS
  section (with compile-verified example), FUNCTIONS section, def removed
  from the BANNED list. Examples auto-compile in test_skill_examples_compile.
- UI: `code` StepKind (cyan, Code2 icon) in types.ts, StepNode.tsx,
  PlaybookEditor.tsx; bundle rebuilt clean (vite).
- Tests: `tests/test_plan004_code_and_functions.py` — 17 tests covering
  compile/round-trip/validation/probes, live runner delegation (fake
  code_run tool: payload, failure stderr, result_error, missing plugin),
  dry-run stubbing, expansion (prefixing, nested lists, outer scope,
  code-in-function rename boundaries), and 14 error cases + depth cap.
  `test_pblang.py` def-ban test updated (classes stay banned).
- Suite: 181 passed. Version 0.16.0 in all three stamps.

## Notes for future work

- Functions are procedures. Passing data out = set a var inside; caller
  reads `vars.<prefix>__<name>`. Return values, inner playbooks (P3), and
  chaining/emit (P4) were explicitly out of scope.
- The stored candidate is the EXPANDED code — editing a playbook created
  with defs shows the flattened steps. Acceptable for now; revisit if
  authors complain.
