# 004 — code() steps and functions

Status: DONE 2026-08-28 — all 4 phases shipped (inline-code-run 0.3.0, playbooks 0.16.0).

Follow-up to plans/003 (0.15.0). Two features from
research/playbook-functions/idea.md, rescoped per owner decisions:

- **code() delegates to plugin-inline-code-run** (the existing bwrap/seatbelt
  jail). No Tier A in-process evaluator — we do not build a second, weaker
  sandbox next to a good one.
- **Functions compile via inline macro expansion** (idea.md P2a). No inner
  playbooks, no chaining.

## Architecture decisions

1. **Cross-plugin call goes through the tool registry, not an import.**
   The playbooks runner already executes tools via
   `self._tools.get(name).handler(**args)` (runner.py:631-638). `code()`
   resolves `code_run` the same way. This sidesteps the managed-loader module
   name problem (`luna_plugin_plugin_inline_code_run`) entirely and reuses the
   probe machinery for absence detection.
2. **JSON results ride a file, not stdout.** The jail discards stdout on
   timeout (inline runner.py:190) and `finalize` deletes the run dir on
   success — so JSON mode writes `outputs/result.json` inside the jail and the
   tool layer reads it back before finalize.
3. **JSON mode wraps user code in a function**, so `return {...}` works (the
   idea.md authoring surface). Inputs are pre-loaded as a dict named `inputs`.
4. **Soft dependency, degrade visible.** A playbook with code() steps
   validates with a warning when inline-code-run is absent, and the existing
   probe promote-gate refuses promote (probes.collect_tools advertises
   `code_run` for code steps). No hard manifest dependency.
5. **Functions are procedures** (no return value in v1). Call args compile to
   namespaced value assignments (`vars.<callid>__<param>`); body steps get ids
   `<callid>__<innerid>`. Bare-name refs inside the body resolve through an
   alias map. Recursion is a compile error; def→def calls allowed (depth
   capped). Codegen round-trips the *expanded* IR (the `code` column stays
   authoritative for source with `def`s, same contract as plans/003).

## Phase 1 — plugin-inline-code-run 0.3.0: JSON run mode

`code_run` tool gains `input_json: object | null` (default null). When
provided:

- tool writes it to `<run_dir>/inputs/input.json` before the run;
- user code is wrapped:
  ```
  import json; inputs = json.load(open('inputs/input.json'))
  def __pb_main__(inputs): <user code, indented>
  json.dump(__pb_main__(inputs), open('outputs/result.json','w'))
  ```
  so `return {...}` is the contract, `inputs` is a plain dict;
- after the run, `outputs/result.json` is parsed into payload key `result`
  (parse/serialization failure → `result_error` key, run still reported
  faithfully).

No jail changes, no new tool, no policy change. Tests: wrapper unit tests +
jailed round-trip (skipif no jail, like the rest of the suite). Version
0.3.0 in all three stamps. Ship: commit, push, publish.

## Phase 2 — plugin-playbooks: `code()` step kind

- definition.py: `StepKind.CODE = "code"`; StepDef fields `source: str | None`
  (the Python body — named `source`, not `code`, to avoid colliding with the
  authoritative `code` column and edit-tool kwargs) and
  `code_inputs: dict[str, Any] | None` (templated values). Reuses common
  `timeout`. No nested bodies → id-collection/cycle helpers untouched.
- pblang: `STEP_FUNCS["code"]`, `_build_code` (first positional = source
  string literal; kwargs `inputs={...}` compiled like tool args,
  `timeout=int`); codegen `RESERVED_NAMES += "code"` + `_step_parts` branch
  (multi-line source rendered as a triple-quoted literal).
- runner.py: `_run_code` — render `code_inputs`, dry-stub by step id
  (stub value = the returned dict), else
  `self._tools.get("code_run").handler(code=source, input_json=inputs,
  timeout_sec=step.timeout or 60, title=...)`; parse the JSON-string tool
  result; ok → step output `{"result": <result>}` (so refs read
  `steps.<id>.result.<key>`); failure → step error with exit code + stderr
  excerpt; missing tool → clear "install plugin-inline-code-run" error.
- validation.py: KIND_KEYS + KIND_OUTPUT_KEYS + `_req` (source required);
  `code_inputs` values template-checked; the source body is NOT
  template-checked (Python braces false-positive). `_check_leaf` warns when
  `code_run` is unregistered.
- probes.py: `collect_tools` emits `code_run` for code steps → promote gate
  refuses when the plugin is absent.

## Phase 3 — plugin-playbooks: functions via macro expansion

- compiler.py refactor: extract the module-level statement loop
  (compile_playbook:866-905) into `_Compiler.compile_stmt_list`, used by both
  module level and def bodies.
- Collect `ast.FunctionDef` (module level only; positional params only, no
  defaults/varargs/kwonly in v1). `def` name must not collide with
  combinators/step ids.
- Call sites (statement position, module level or inside then/else_/body/
  branches lists): `notify(...)` where notify is a def → expand:
  - each arg compiles to a value assignment `state set <call>__<param>`
    (f-strings/Jinja/exprs all work, uniform with `x = expr`);
  - body compiled with id prefix `<call>__` via an alias frame
    (`name→id` map consulted by `_transform_expr` for both `steps.` and
    `vars.` rewrites; value names inside the body also prefixed since vars
    are run-global);
  - body AST deep-copied per expansion (`_transform_expr` mutates in place);
  - call id: assigned name (`x = notify(...)` is an error — procedures) —
    default `notify`, `notify_2`, … via `_gen_id`.
  - nested list expansion: compile_step_list splices multi-step expansions.
- `ast.Return` inside a def → compile error with a hint ("functions are
  procedures; write results with a value assignment and read vars.<name>").
  Recursion (direct or transitive) → compile error. def→def calls allowed.
- Remove `def` from the BANNED list; typo'd/unknown call error message gains
  "or define a function with def".

## Phase 4 — reference, skill, UI, e2e, ship

- reference.py cheatsheet: code() line in COMBINATORS, output shape line,
  FUNCTIONS section; intro "no def" line updated.
- Skill body: code() in THE LANGUAGE combinator list + a CODE STEPS section
  (when to use vs Jinja: loops/reshapes/multi-step logic → code; single
  pluck/format → Jinja), def section replacing the ban; skill examples
  auto-compile via test_skill_examples_compile.
- ui-src: StepKind union + icon/badge/label for `code`; rebuild ui/ if the
  toolchain builds cleanly, else ship without (unknown-kind rendering
  degrades to default) and note it.
- Tests: e2e dry run (code step stubbed), jailless unit coverage for
  compiler/codegen/validation/probes; full suite green.
- Version 0.16.0 in all three stamps. Commit, push (huemorgan2), publish
  both plugins, verify catalog, remind to upgrade installed agents.

## Non-goals

Inner playbooks (P3), emit/async-subtask chaining (P4), function return
values, code() Tier A evaluator, hard manifest dependencies.
