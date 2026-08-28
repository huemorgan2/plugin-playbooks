# Phase 1 — Foundations

## Scope

1. **Baseline**: record pre-change test state (ui-src vitest, plugin pytest).
2. **types.ts sync** with backend `definition.py`: add `source`, `code_inputs`,
   `count`; keep field order mirroring the pydantic model.
3. **Test tooling**: add `jsdom`, `@testing-library/react`, `@testing-library/jest-dom`
   devDeps; vitest config for a jsdom environment for `.tsx` tests (existing `.ts`
   tests keep running in node env).
4. **Primitives** in `ui-src/src/playbooks/explain/primitives.tsx`:
   - `Expr` — Jinja/expression tokenizer + semantic colors (`inputs.*` blue,
     `steps.*` violet, `vars.*` emerald, literals amber, operators dim).
   - `Value` — typed literal chip; auto-`Expr` when a string contains `{{`.
   - `KVTable` — key/value grid, nested objects expand.
   - `SchemaTree` — JSON-Schema field tree.
   - `Code` — Prism-highlighted read-only block (bundled, python grammar).
   - `StepList` — one-line nested-step rows, click selects the node on canvas.
5. **Panel regrammar** in `StepDetailPanel`: eyebrow (kind, human-spelled, kind
   color) → generated headline (`headline(step)` per kind, data-derived) → support
   (`step.explanation`, marked as author note) → detail area → collapsed
   "Raw definition" toggle. Width 320 → 420px.
6. **Proving-pair explainers** + registry `STEP_EXPLAINERS`: `tool_call` and
   `code` (fixes the invisible `source` bug). Other kinds fall back to the
   existing `stepDetailRows` list inside the new grammar until phase 2.

## Deliverables

- `ui-src/src/playbooks/explain/primitives.tsx`, `explain/registry.tsx`,
  `explain/toolCall.tsx`, `explain/code.tsx`, `explain/headline.ts`
- Updated `types.ts`, `PlaybookEditor.tsx`, `package.json`, vitest config
- First render tests: Expr tokenizer, Value, tool_call + code explainers
  (screen tokens ⊆ fixture; absent field ⇒ absent row)

## Verification

- `npm run build` (tsc + vite) clean; `npm test` green including new tests.
- Full plugin pytest green (no regressions vs baseline).
- Real-Luna check deferred to the phase that ships a version (UI bundle is
  rebuilt then); noted in the summary.

## Ship

Batched: phase 1 alone leaves the panel half-converted, so version bump +
publish happen with phase 2 (allowed by the skill for non-standalone phases).
Commit at end of phase regardless.
