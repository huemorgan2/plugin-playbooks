# Phase 3 — extract the canvas + code views

## Baseline (2026-08-30, after phase 2)

- plugin-playbooks `b54dd52`. pytest 283, vitest 111, `tsc --noEmit` clean.
- No component tests exist yet (the 13 vitest files are pure `.ts`);
  `@testing-library/react` + `jsdom` are installed and the vitest env is
  jsdom, so rendering is possible.

## Scope

UI-only refactor, no visible change, ships in 0.28.0.

New `ui-src/src/playbooks/VersionCanvas.tsx` with three exports:

1. `CanvasSurface` — presentational: the name + "About this playbook"
   overlay, the past/live-run banner (with its clear button), the
   `ReactFlow` block (dots background, controls, minimap hidden while a run
   is overlaid), the empty-playbook placeholder, and an `overlay` slot for
   anything the caller wants floating on top (today: the live/candidate
   switch, which phase 4 deletes). Fully controlled: takes `nodes`, `edges`,
   change handlers, click handlers. This is exactly the editor's canvas
   branch (`PlaybookEditor.tsx` ~583-710) lifted out.
2. `VersionCanvas` — owns its own `useNodesState`/`useEdgesState`, builds
   the graph from a `PlaybookDef` (+ optional `PlaybookRunDetail` for
   status colouring) with `buildGraph`, and renders `CanvasSurface`. Step
   click is reported up (`onSelectStep`) so the caller can show
   `StepDetailPanel`. This is what phase 4's Versions tab uses per version.
3. `CodeView` — the read-only code block (`Code` + the "agent writes
   this" footer), from the editor's `code` branch (~713-740).

`PlaybookEditor.tsx` renders `CanvasSurface` and `CodeView` in place of
the inlined JSX; its nodes state, live-patch glow queue and run overlay
logic stay where they are (they write into that nodes state). The
live/candidate switch is passed through the `overlay` slot. No behaviour
or class changes.

## Tests (vitest, first component tests in the repo)

- `VersionCanvas` renders a node per step of a 2-step def (jsdom needs a
  `ResizeObserver` stub for ReactFlow) and the playbook name overlay.
- `VersionCanvas` with `runDetail` renders the "Past run" banner and
  hides the minimap; the clear button calls `onClearRun`.
- `CodeView` renders the source and the agent-name footer.
- Empty def → "Empty playbook" placeholder.

## Verification

`tsc --noEmit`, vitest green (111 + new), `npm run build` succeeds (dist
not committed until phase 7). No real-environment step (no visible change).
