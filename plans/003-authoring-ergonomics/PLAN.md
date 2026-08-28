# 003 — authoring ergonomics: stop agents from burning 30 minutes on an if statement

Status: SHIPPED 2026-08-28 in 0.15.0. See execution_summary.md.

Owner ask: a real transcript (job-scan playbook, 2026-08-28) showed an agent
spending ~60–80 tool calls and 15 playbook versions on work that should take
minutes. A second transcript (candidate-intake) showed 165s of reasoning spent
on Jinja filter trivia. Diagnose → fix. All four causes are in this plugin;
nothing is a luna-core issue.

## The four observed failure modes

1. **The language reference evaporates.** The whole spec lives in the
   `playbook-authoring` skill body, loaded once. After context compaction in a
   long session the agent guesses: "is `if_` supported?", "does `equalto`
   exist?", and — flatly wrong — "there is no variable assignment" (it never
   found `state()`/`vars`). Each guess costs a full edit→validate→dry_run
   cycle. Nothing on the tool surface can bring the spec back.
2. **Jinja attribute-first lookup poisons documented paths.** The skill
   teaches `steps.<id>.result.messages`, but `steps` is a plain nested dict,
   so `steps.fetch.result.items` returns the dict's `.items()` *method*. The
   `_VarsView` fix (007.009.01) covers only top-level `inputs`/`vars`. The
   resulting error ("'builtin_function_or_method' object has no attribute
   'name'") surfaces steps later with no hint.
3. **No regex, no documented filter set.** `_SANDBOX_ENV` is a stock
   SandboxedEnvironment. Phone normalization became a chained-`replace`
   research project; the agent had no reliable model of which filters exist.
4. **No computed values.** `x = <expression>` is rejected ("Only step calls
   can be assigned"), so derived values get re-inlined as Jinja at every use
   site. `state(set_('x', ...))` → `vars.x` exists but is invisible (see 1)
   and clumsy. (From research/playbook-functions/idea.md — we take ONLY this
   item; code()/functions/inner-playbooks/chaining stay out of scope.)

A fifth (spec stubs silently never firing on a bad key) already shipped in
0.14.2.

## What ships — 0.15.0

### Phase 1 — item-first template resolution (runner.py)

A `SandboxedEnvironment` subclass whose `getattr(obj, attribute)` treats
Mappings as data, full stop: key present → the value; key absent → undefined
(loud under StrictUndefined). Dict methods are never reachable via `.attr`,
at every depth — `steps.fetch.result.items` now means the key, matching what
the skill has always taught. `_VarsView` stays (harmless, and non-Mapping so
it keeps its own path). `compile_expression` overlays preserve the subclass,
so `_eval_expression` gets the same semantics.

Trade-off accepted: a template can no longer call real dict methods on step
outputs (`.items()`/`.keys()` etc. as calls). Correct answer is Jinja
(`| items`, iteration) — and today those calls are the bug, not the feature.

### Phase 2 — regex filter kit (runner.py)

`regex_replace(value, pattern, replacement='', count=0)`,
`regex_search(value, pattern, group=0)` (no match → ''),
`regex_findall(value, pattern)` — registered on the runner env. The builtin
set (selectattr, map, tojson, …) already covers the rest; the gap was regex.

### Phase 3 — value assignment `x = expr` (pblang/compiler.py)

An Assign whose RHS is NOT a combinator call compiles to a `state` step:
`{kind: state, id: x, state: [{op: set, var: x, value: <eval_value(expr)>}]}`.

- Read back as `vars.x` in Jinja strings; bare `x` in bare-Python expressions
  resolves to `vars.x` (a `value_names` set checked in `_transform_expr`,
  before `step_ids`).
- RHS forms: literals, bare expressions (via `eval_value`), f-strings (via
  `template_value`), raw Jinja strings (pass through, like state op values).
- RHS that is a Call: combinator → step (unchanged); `range(...)` → value;
  anything else keeps the "Unknown step function" error (a typo'd combinator
  must not silently become an expression) with an added hint about value
  assignment.
- `x` joins the step-id namespace (duplicate = same error as today). Runner
  untouched — it's a plain state step. Codegen round-trips as
  `state(set_(...))`, acceptable: the `code` column is authoritative.

### Phase 4 — the reference is recallable (reference.py, agent_tools.py, skill)

- New `plugin_playbooks/reference.py`: `LANGUAGE_CHEATSHEET` — a compact
  (~70 line) spec: combinators + exact kwargs, value assignment, reference
  shapes, `vars`/`state()`, the COMPLETE filter/test list (builtins that
  matter + the regex kit), condition/expression rules, the authoring loop.
- Attach it where a lost agent actually re-enters:
  - `playbook_edit` READ stage → `language_reference` field (every edit
    begins here; this is the recall point).
  - `playbook_validate` and `playbook_propose` results, on compile/validation
    errors only.
  - New tool `playbook_language_reference` (auto_approve, low risk, in the
    skill's tool group) for on-demand recall.
- Skill body: document `x = expr`, the filter list (pointing at the tool for
  the full sheet), and that `.field` on step outputs always reads the key.

### Version stamps

0.15.0 in all three: `__init__.py` PluginManifest, `pyproject.toml`,
`luna-plugin.toml` (in-code manifest is authoritative).

## Tests

- Phase 1: `.items`/`.keys`/`.values` as data at depth; missing key raises in
  strict eval; existing template tests stay green.
- Phase 2: each regex filter, in templates and in `_eval_expression`.
- Phase 3: compile `x = expr` → state IR; bare-name reference rewrite;
  duplicate id; typo'd-combinator error unchanged; f-string RHS; skill
  examples still compile (`test_skill_examples_compile`).
- Phase 4: read-stage carries `language_reference`; validate attaches it on
  error and not on success; new tool returns the sheet.

## Non-goals

`code()` steps, `def` functions, inner playbooks, emit/async-subtask chaining
(research/playbook-functions/idea.md P1–P4 remainder). No schema change, no
migration, no luna-core change.

## Ship

Full test suite → commit → push (huemorgan2) → publish 0.15.0 to
marketplaces.com.ai.
