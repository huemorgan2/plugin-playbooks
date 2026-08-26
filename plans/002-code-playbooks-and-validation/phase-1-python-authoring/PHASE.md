# Phase 1 — Python authoring layer

Scope: the agent authors playbooks in restricted Python; the stored/validated/
executed format stays the `PlaybookDef` IR. AST-parsed, never executed.

## Deliverables

1. `plugin_playbooks/pblang/` package:
   - `compiler.py` — Python source → `PlaybookDef`. `ast.parse` + a
     whitelist walker. Errors carry line numbers and a fix hint
     (`CompileError(line, message, hint)` collected, all-at-once like the
     validator).
   - `codegen.py` — `PlaybookDef` → Python source. Variable names = step ids
     (id-preserving round-trip). Header comment block carries
     name/display_name/description/when_to_use/inputs/triggers as a
     `playbook(...)` declaration call.
2. Language surface (maps 1:1 to StepKind):
   - `playbook(name=..., description=..., inputs={...}, triggers=[...])`
     module-level declaration (once, first statement).
   - `x = tool("tool_name", arg=..., ...)` → tool_call (id=x)
   - `x = llm("prompt", output={...}, purpose=..., model=..., system=...,
     data=...)` → llm_step
   - `x = agent("prompt", output={...}, tools=[...])` → agent_step
   - `if_(cond, then=[...], else_=[...], id=...)` / `loop(over=..., body=[...],
     ...)` / `parallel([...], ...)` / `approve(show=[...])` /
     `wait_event("event", ...)` / `subtask("playbook", inputs={...},
     returns={...})` / `state(...)` / `halt(when=..., value=...)`
   - Step options via keyword args: `retry=`, `on_error=`, `timeout=`,
     `explanation=`.
   - Expressions: `inputs.x`, `x.field`, comparisons/arithmetic/f-strings
     compile to Jinja template strings against the existing namespace.
   - Nested steps (then/else_/body/branches) are lists of the same calls;
     assignments inside give ids, bare calls get generated ids.
3. Tools updated:
   - `playbook_propose(name, code=...)` and `playbook_edit(name, code=...)`
     accept code; `definition_yaml` stays accepted this phase (dies in
     phase 7 after migration).
   - `playbook_get_definition` returns code (stored or codegen'd) by
     default, IR YAML via `format="yaml"`.
   - Snippet-diff editing: `playbook_edit(name, old=..., new=...)` applies a
     unique-match text replacement to the current code then compiles.
4. Storage: `code TEXT` nullable column on `playbooks` + `playbook_versions`
   (added via the plugin's own create-on-enable path; SQLite/PG safe).
5. Migration: on plugin load, any playbook without code gets codegen'd code
   stored, after a `compile(codegen(ir)) == ir` verification; mismatch →
   log + leave code NULL (derive on read).
6. Tests: compiler unit tests per construct; codegen round-trip over every
   test fixture + property `compile(codegen(ir)) == ir`; error-message
   tests (line numbers, banned constructs); tool-level tests for code
   propose/edit/snippet-diff.

## Out of scope (later phases)

Manifest/ticket flow (2), candidate versions (3) — this phase edits live, as
today, to keep the diff reviewable.

## Verification

- Full pytest suite green (old 29 + new).
- QA Luna: sync, restart, `playbook_propose` with code creates a playbook;
  `playbook_get_definition` returns code; dry_run of a code-authored playbook
  works; canvas renders it (API check of definition JSON; browser check in
  phase 6).

## Version

0.8.0.
