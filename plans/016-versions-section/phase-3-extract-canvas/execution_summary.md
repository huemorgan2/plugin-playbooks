# Phase 3 — extract the canvas + code views — execution summary

**In the repo, unreleased** (ships in 0.28.0 with phases 4–6). Stamps stay
at 0.27.1. No user-visible change.

## What changed

- New `ui-src/src/playbooks/VersionCanvas.tsx`:
  - `CanvasSurface` — controlled canvas: name/"About this playbook"
    overlay, run banner ("Live run"/"Past run" + clear button), ReactFlow
    with dots background, controls, minimap (hidden while a run is
    overlaid), empty-playbook placeholder, and an `overlay` slot.
  - `VersionCanvas` — owns its nodes/edges state for one `PlaybookDef`
    (+ optional `PlaybookRunDetail` for status colouring), re-layouts when
    either changes, reports step clicks via `onSelectStep`.
  - `CodeView` — read-only source block with the "{agent} writes this"
    footer and an optional `header` slot; `sourceFor(code, def)` helper.
- `PlaybookEditor.tsx` (1268 → 1177 lines): the inlined canvas and code
  branches are replaced by `CanvasSurface` / `CodeView`; the live/candidate
  switch is passed through the `overlay` / `header` slots; the editor's
  nodes state, live-patch glow queue and run overlay logic are unchanged.
  Dropped now-unused imports (`ReactFlow`, `Background`, `Controls`,
  `MiniMap`, `BackgroundVariant`, `StepNode`, `TriggerNode`, `Code`,
  `Workflow`, `Copy`) and the `copied`/`explainOpen` state that moved into
  `CanvasSurface`.
- First component tests in the repo:
  `ui-src/src/playbooks/__tests__/VersionCanvas.test.tsx` (4 tests; stubs
  `ResizeObserver`/`DOMMatrixReadOnly`/offset sizes so ReactFlow renders
  under jsdom).

## Verification

- `tsc --noEmit` clean; vitest **115 passed** (111 + 4).
- `vite build` to a scratch dir succeeds (the committed
  `plugin_playbooks/ui/` dist is left at 0.27.1 until phase 7).
- No real-environment step (no visible change).

## Deviations from PHASE.md

None.

## Reassessment of remaining phases

- Phase 4: `VersionsTab` should render `VersionCanvas` per selected
  version and reuse `StepDetailPanel`, which is still a private function
  in `PlaybookEditor.tsx` — export it (and `TabBtn`, `timeAgo`) rather
  than duplicating. The Runs-tab "show on canvas" path becomes: select
  the run's version, then set that version's `runDetail`; the editor's
  own run-attach effect (`playbook.run.started` → load onto canvas) must
  move into `VersionsTab` too, otherwise a live run would try to colour a
  canvas that no longer exists in the editor.
- Phases 5–7 unchanged.
