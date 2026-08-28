# 003 — execution summary (0.15.0, 2026-08-28)

All four phases shipped as planned; one addition beyond the plan.

## What changed

- **Phase 1** (`runner.py`): `_PlaybookEnvironment(SandboxedEnvironment)` —
  `getattr` on any Mapping reads the key (missing key → undefined, loud under
  StrictUndefined). Dict methods are unreachable via `.attr` at every depth;
  `steps.<id>.result.items` now always means the data. `_VarsView` kept.
- **Phase 2** (`runner.py`): filters `regex_replace`, `regex_search`,
  `regex_findall` — plus `split`, added when a sheet-audit showed it's the one
  commonly-assumed filter Jinja lacks (everything else listed was verified
  present in the env).
- **Phase 3** (`pblang/compiler.py`): `x = <expr>` (non-combinator RHS, and
  `range(...)`) compiles to a state set op with id `x`; `(x := <expr>)` works
  in body/then/else_/branches. `value_names` set rewrites bare `x` →
  `vars.x` in later expressions (checked before `step_ids`). F-string RHS via
  `template_value`. Typo'd combinators still error on the step-call path;
  the "Unknown step function" hint now teaches value assignment.
- **Phase 4**: new `reference.py` with `LANGUAGE_CHEATSHEET` (filter list
  audited against the live env). Attached on: `playbook_edit` READ stage,
  `_compile_code` error payloads (propose + edit-write), failed
  `playbook_validate` (not on green). New auto-approve tool
  `playbook_language_reference`, added to the SkillDef tool group and
  `AUTHORING_TOOLS`. Skill body: value-assignment section, dot-reads-key
  rule, regex filters, pointer to the tool.
- Version 0.15.0 in all three stamps (`__init__.py`, `pyproject.toml`,
  `luna-plugin.toml`).

## Verification

`tests/test_authoring_ergonomics.py` (15 tests): item-first at depth, loud
missing keys, `compile_expression` semantics, all four filters, IR shape of
value assignment, bare-name → vars rewrite, f-string/Jinja RHS, walrus in
nested body, typo + duplicate errors, reference on read-stage/failed-validate
(and absent on green), the tool, and an end-to-end dry run: phone
normalization via regex filters into a `vars` read inside a branch gated on
`steps.rows.result.items | length` — the exact chain both incident
transcripts failed on. Full suite: **164 passed**.

## Out of scope (unchanged from plan)

`code()` steps, functions, inner playbooks, chaining
(research/playbook-functions/idea.md) — revisit after 0.15.0 lands in real
authoring sessions.
