# 010 — Step explain panel: per-kind renderers, zero hardcoded data

## Problem

Clicking a step on the canvas opens `StepDetailPanel` (PlaybookEditor.tsx), which renders
`stepDetailRows()`: a flat label/value list where every structured field (`args`,
`output_schema`, `event_filter`, `inputs_map`, `returns`, state ops, halt value) is
`JSON.stringify(...)` into a `<pre>`. A condition shows its raw `when` string; the user
has to decode Jinja and JSON themselves. Two concrete gaps found while auditing:

1. **`code` steps show no code.** `StepDef.source` and `code_inputs` (definition.py:163-168)
   are missing from ui-src `types.ts` and from `stepDetailRows` — the one kind whose whole
   point is code renders an empty "No static config" panel.
2. **types.ts has drifted from definition.py** (`source`, `code_inputs`, `count` vs unused
   fields) and nothing catches drift, which is exactly how gap 1 happened.

## Goal

Every step kind gets a dedicated **explain renderer**: a colored, structured visualization
of that block, derived 100% from the step definition (and, when a run is loaded, the run
record). No renderer contains data — no sample values, no fallback text standing in for
content, no agent-written prose required. A field that is absent simply doesn't render.
What the user sees is provably what the block is.

Layout follows `/vision/ux_guidelines.md`: eyebrow (kind) → bottom-line headline →
support line → expandable detail. Reuse the existing kind palette (`STEP_COLORS`).

## Design

### 1. Panel grammar (restructure `StepDetailPanel`)

- **Eyebrow**: kind, spelled for humans (`CONDITION`, `TOOL CALL`), in the kind color.
- **Headline**: one sentence **generated from the definition** by a per-kind
  `headline(step)` function — e.g. condition: `Branches on order.total > 100`;
  loop: `For each url in steps.crawl.result`; tool_call: `Calls web_search`.
  Never sourced from `step.explanation` (agent prose can drift from the code).
- **Support**: `step.explanation` if present, dimmed, labeled as the author's note.
- **Detail**: the per-kind explain component (below), always visible (it IS the point),
  with raw JSON of the whole StepDef behind one collapsed "Raw definition" toggle at the
  bottom — the escape hatch, never the primary surface.
- Widen the panel to ~420px (320px cannot hold code); `code` steps may expand wider.

### 2. Shared primitives (`ui-src/src/playbooks/explain/primitives.tsx`)

- **`Expr`** — renders a Jinja/template expression with a tiny tokenizer and semantic
  colors: `inputs.*` blue, `steps.*` violet, `vars.*` emerald, string/number literals
  amber, operators/keywords dim. Used for `when`, `over`, `while`, `until`,
  `break_when`, `collect`, `if`, and for `{{ ... }}` spans embedded in strings.
- **`Value`** — a literal rendered as a typed chip (string / number / bool / null each
  its own color); auto-switches to `Expr` when the string contains `{{`.
- **`KVTable`** — key/value grid for `args`, `code_inputs`, `event_filter`,
  `inputs_map`, `returns`. Values via `Value`; nested objects expand inline.
- **`SchemaTree`** — JSON-Schema (`output_schema`) as a field tree: name, type badge,
  required dot, description text. No raw JSON.
- **`Code`** — read-only syntax-highlighted block (in-repo ~60-line tokenizer, no
  dependency — changed from Prism in phase 1), Python grammar for `source`, also
  reused by the Code tab for pblang.
- **`StepList`** — one-line rows for nested steps (`then`/`else`/`body`/`branches`):
  kind icon + id in kind color; clicking selects that node on the canvas.

### 3. Per-kind explainers (`ui-src/src/playbooks/explain/<kind>.tsx`)

Registry `STEP_EXPLAINERS: Record<StepKind, FC<{step, onSelectStep}>>`; panel dispatches
by `step.kind`. Each receives ONLY the step. Sketch:

| kind | rendering |
|---|---|
| `tool_call` | tool pill + `KVTable(args)` |
| `agent_step` | prompt as prose with `{{ }}` spans highlighted; tools as chips; `SchemaTree` |
| `llm_step` | prompt + system; purpose/model badges; `SchemaTree` |
| `condition` | `IF` block: `Expr(when)`; two colored branch rows `then → StepList` / `else → StepList` |
| `loop` | sentence assembled from real fields (`for each {item_name} in Expr(over)`); guard rows for while/until/break_when; concurrency, max_iterations, collect chips; `StepList(body)` |
| `parallel` | branch cards (`StepList` each) + fan_in badge |
| `wait_for_event` | event pill + `KVTable(event_filter)` + timeout |
| `wait_for_approval` | `show` fields as chips |
| `subtask` | playbook pill; `inputs_map` as parent→child arrows table; `returns` table |
| `state` | one row per op: op badge (set/append/incr/pop…), var chip, `Value`, `into` arrow |
| `halt` | guard `Expr(when)` if any + returned `Value` |
| `code` | `Code(source)` full-width + `KVTable(code_inputs)` + "returns → steps.<id>.result" line |

Common footer (all kinds): retry / on_error / timeout as small chips, only when non-default.

### 4. Derived data-flow section (new)

Parse `inputs.*` / `steps.*` / `vars.*` references out of all the step's expressions and
render **Reads** / **Writes** chip rows (writes: `steps.<id>.result`, state vars, loop
`collected`). 100% derived, answers the real question behind "I need to figure out the
JSON" — where a block's data comes from and goes. Clicking a `steps.x` chip selects that
node.

### 5. Zero-data guarantee (enforced, not promised)

- Explainers take only `step` — no defaultProps carrying content, no placeholder strings
  standing in for data (structural labels like "Reads" are fine).
- **Render tests** (vitest + jsdom + @testing-library/react, new devDeps): for each kind,
  build fixture StepDefs and assert (a) every data token on screen exists in the fixture,
  (b) omitting a field removes its row entirely.
- **Drift test**: a JSON list of StepDef field names exported from `definition.py`
  (small pytest writes/checks a `stepdef_fields.json` checked into ui-src); a vitest test
  asserts every field is either consumed by some explainer or on an explicit ignore list.
  This is what would have caught the invisible `code.source` today.

### 6. Adjacent fix-ups

- Sync `types.ts` with `definition.py` (`source`, `code_inputs`, `count`).
- Code tab: highlight pblang with the same `Code` primitive (Python grammar is close
  enough) instead of a plain `<pre>`.
- Run "raw input/output" stays, but rendered as a collapsible JSON tree (keys bold,
  values typed-colored) instead of a stringified blob.

## Phases

1. **Foundations** — types.ts sync; test tooling (jsdom, testing-library); primitives
   (`Expr`, `Value`, `KVTable`, `SchemaTree`, `Code`, `StepList`); panel regrammar
   (eyebrow/headline/support/detail + raw-definition toggle + width). `tool_call` +
   `code` explainers as the proving pair (simplest + the broken one).
2. **All kinds** — remaining 10 explainers, per-kind headlines, data-flow section,
   common footer chips; delete `stepDetailRows`.
3. **Guarantees + polish** — fixture render tests per kind, drift test against
   definition.py, Code-tab highlighting, JSON-tree for run raw data. Verify on a real
   running Luna (canvas click-through of every kind). Version bump (manifest in code +
   toml + package.json), push, publish.

## Suggested but deferred (want a decision)

- **Hover-to-highlight**: hovering a `steps.x` chip glows node `x` on the canvas.
  Cheap once data-flow exists; slight clutter risk.
- **Step → code-tab mapping**: clicking a step highlights its region in the pblang
  source. Needs source maps from codegen — real backend work, separate plan.
- **Resizable panel** via drag handle instead of fixed widths.
