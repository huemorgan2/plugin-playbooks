# Phase 2 — All kinds, data flow, footer

## Scope

1. Explainers for the remaining 10 kinds (in `explain/registry.tsx`, splitting
   files only if it grows unwieldy):
   - `agent_step`: prompt via `TemplateText`, tools chips, `SchemaTree`.
   - `llm_step`: prompt + system via `TemplateText`, purpose/model badges, `SchemaTree`.
   - `condition`: IF block — `Expr(when)`, colored then/else rows with `StepList`.
   - `loop`: iteration sentence from real fields, guard rows (`while`/`until`/
     `break_when`), chips (concurrency>1, max_iterations, collect, item_name),
     `StepList(body)`.
   - `parallel`: branch cards (`StepList` each) + fan-in badge.
   - `wait_for_event`: event pill, `KVTable(event_filter)`, timeout.
   - `wait_for_approval`: `show` chips.
   - `subtask`: playbook pill, `inputs_map` arrows table, `returns` table.
   - `state`: one row per op — op badge, var chip, `Value`, `into` arrow.
   - `halt`: guard `Expr(when)` + returned `Value`.
2. **Data-flow section** (`explain/dataflow.ts` + panel section): derive
   **Reads** (via `extractRefs` over every expression-bearing field, incl. args /
   code_inputs / filters / maps / prompts / state values, recursively) and
   **Writes** (`steps.<id>.result`, `steps.<id>.collected`, subtask `returns`
   keys, state vars incl. `into`). Chips colored by root; clicking a `steps.x`
   chip selects that step's node (id lookup walks the definition tree).
3. **Common footer chips**: retry, on_error≠abort, timeout/timeout_seconds —
   rendered for every kind from the panel (not per-explainer).
4. Delete `stepDetailRows` + `fmtStateOp` from PlaybookEditor (legacy fallback gone).
5. Render both `timeout` and `timeout_seconds` (types.ts gains `timeout`).

## Deliverables

- Extended registry (12/12 kinds), `explain/dataflow.ts`, panel data-flow +
  footer sections, legacy row code removed, types.ts `timeout` added.
- Render tests per new kind (same zero-data assertions), dataflow unit tests.

## Verification

- `npm run build` clean; vitest green; pytest green (regression).
- Real-Luna CDP browser check of the panel across kinds (ship phase for 1+2).

## Ship

Version 0.17.0 (all three stamps), commit, push, publish to marketplace.
