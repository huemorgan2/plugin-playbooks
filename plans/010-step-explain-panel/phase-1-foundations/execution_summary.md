# Phase 1 — Foundations: execution summary

## Baseline (pre-change)

- ui-src vitest: 5 files, 33 tests, all passing.
- plugin pytest: 181 passed.
- Repo clean at 0.16.0 (commit 101eb43).

## What shipped

New directory `ui-src/src/playbooks/explain/`:

- `tokens.ts` — pure tokenizers (testable in node env): `tokenizeExpr` (Jinja/python
  expressions; classifies `inputs.*`/`steps.*`/`vars.*` references, strings, numbers,
  keywords), `splitTemplate` (prose vs `{{ }}` spans), `tokenizePython` (line-oriented,
  handles triple-quoted strings), `extractRefs` (template references for the phase-2
  data-flow section).
- `primitives.tsx` — `Expr`, `TemplateText`, `Value` (typed literal chips, template
  strings delegate to `Expr`), `KVTable` (nested objects recurse), `SchemaTree`
  (JSON-Schema field tree with type badges/required dots/enums), `Code` (line-numbered
  highlighted python), `StepList` (nested-step rows that select the node on canvas),
  `KIND_LABELS`, `KIND_ICONS`/`kindIcon`, `SectionLabel`, `NamePill`.
- `headline.ts` — per-kind bottom-line generator, definition fields only (explicitly
  never `step.explanation`).
- `registry.tsx` — `STEP_EXPLAINERS` registry with the proving pair: `tool_call`
  (tool pill + args table) and `code` (inputs table + highlighted source + derived
  `steps.<id>.result` note). **Fixes the bug where `code` steps showed no code.**

Changed:

- `types.ts` — synced with backend `definition.py`: added `source`, `code_inputs`;
  `over` widened to `string | any[]` (backend allows literal lists).
- `PlaybookEditor.tsx` — `StepDetailPanel` regrammar per ux_guidelines: eyebrow
  (human kind label in kind color + step id) → generated headline → explanation as
  an indented author note → explainer (or legacy rows for unconverted kinds) →
  collapsed "Raw definition" toggle. Width 320 → 420px. Local `KIND_ICONS` removed
  in favor of the shared one; panel now receives `onSelectStep`.
- `package.json` — devDeps: `jsdom`, `@testing-library/react`, `@testing-library/jest-dom`.
  jsdom is enabled per-file via the `// @vitest-environment jsdom` pragma; existing
  `.ts` tests keep the node environment.

Tests added: `explain/__tests__/tokens.test.ts` (tokenizers reassemble input exactly,
classification, ref extraction), `headline.test.ts` (per-kind, no-prose rule,
truncation), `explainers.test.tsx` (jsdom render tests: screen tokens come from the
fixture; absent field ⇒ absent section; KVTable/SchemaTree/Value behavior).

## Verification

- `npm run build` (tsc + vite) clean; bundle rebuilt into `plugin_playbooks/ui/`.
- vitest: 8 files, 55 tests passing (33 baseline + 22 new).
- pytest: 181 passed — no regressions.
- Real-Luna browser check deferred to the phase-2 ship (panel is half-converted
  until all kinds have explainers), as planned in PHASE.md.

## Deviations from PHASE.md

- **No Prism.** The plan named prismjs; implemented a ~60-line in-repo tokenizer
  instead — no dependency, tailwind-ink colors natively, trivially testable, and
  read-only display doesn't need grammar-perfect highlighting. PLAN.md §2 updated.
- Files landed as `tokens.ts`/`registry.tsx` rather than one file per explainer;
  per-kind explainers in phase 2 will extend `registry.tsx` (split only if it
  grows unwieldy).

## Version / commit

No version bump — batched with phase 2 (panel would ship half-converted alone).
Committed on plugin-playbooks main (commit recorded in the phase-2 summary if
amended; see `git log` for hash).

## Reassessment of remaining phases

- Phase 2 unchanged in scope. `extractRefs` already exists, so the data-flow
  section is cheaper than planned.
- Phase 3: the drift test should compare backend `StepDef.model_fields` against
  the fields consumed by explainers via an exported JSON; while syncing types.ts
  I noted `timeout` (backend) vs `timeout_seconds` (UI type) — definition.py has
  BOTH `timeout_seconds` (wait_for_event) and common `timeout`; the UI type only
  has `timeout_seconds`. Phase 2 should render both; phase 3's drift test would
  have caught this too.
- PLAN.md §2 edited: Prism replaced by the in-repo tokenizer (marked phase 1).
