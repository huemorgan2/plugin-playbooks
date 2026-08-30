# Phase 4 — the Versions tab

## Baseline (2026-08-30, after phase 3)

- plugin-playbooks `1e623d0`. pytest 283, vitest 115, tsc clean.

## Scope

The user-visible rework (Part B). Ships in 0.28.0.

### Editor (`PlaybookEditor.tsx`)

- Non-draft playbooks: top-level tabs are **`Versions` · `Settings`**
  (both normal `TabBtn`s with icon + word; `Versions` default). The
  icon-only History and Settings buttons, the header
  `Promote candidate vN` button, the side `VersionsPanel`, the side
  `SettingsPanel`, the live/candidate canvas switch and the
  `canvasSource` / `candidateDef` / `candidateCode` plumbing are removed.
- Settings is a full page (`SettingsTab`) with the existing "Agent can
  trigger" section; phase 6 adds "Publish / Promote settings" to it.
- Drafts are untouched: `Canvas` · `Code` tabs, promote-draft button,
  live-patch glow on the editor's own canvas.
- Live agent edits on a playbook: the editor keeps its patch queue and
  forwards each applied patch to `VersionsTab` (`patch` prop), which
  applies it to the selected version's def when that version is the
  candidate (or live when no candidate exists yet) with the glow, exactly
  as the old single canvas did. A `replace` patch / `playbook.saved`
  triggers `loadData()` → `VersionsTab` re-lists and re-fetches the
  selection.

### `VersionsTab.tsx` (new)

- Right pane (w-[300px]): list from `GET /versions`, newest first. Row:
  `vN` bold; badge **top-right**: `Published · Live` (emerald) or
  `Candidate` (violet), nothing otherwise; message; author · date · runs;
  `← promoted from vX`. Selected row = highlighted (`bg-luna-600/20`,
  `text-luna-200`, left accent border), never a "selected" word. Initial
  selection = the live entry.
- Left pane: toolbar `vN` (text-lg bold) · created date (absolute,
  `title` = time-ago) · segmented `Canvas | Code | Manifest | Tests | Runs`
  · right slot: live → static `Published · Live` badge; else
  **`Promote to live`** (Rocket) → `promoteCandidate` for the candidate
  row, `promoteVersion(N)` otherwise; refusal banner under the toolbar via
  `promoteRefusalMessage`; on success `onPromoted()` → editor reloads,
  list re-fetches, selection = new live.
- Views: Canvas = `VersionCanvas` (+ `StepDetailPanel` on step click, moved
  to its own file); Code = `CodeView`; Manifest = `ManifestTab` for the
  live version (editable, saving bumps live as today), read-only snapshot
  for others; Tests = `TestsTab` (unversioned until phase 5); Runs =
  `RunsTab` with `version` prop (`?version=N`). "Show on canvas" from Runs
  → Canvas view of the same version with the run overlaid. Live run
  attach (`playbook.run.started` for this playbook) moves here: select the
  run's version and overlay it — needs `playbook_version` on run
  summaries/detail (add to `list_runs` / `get_run` responses + types).
- Per-version run counts on the rows refresh on `playbook.run.*` events.

### Tests

- vitest `VersionsTab` (mocked `playbooksApi`): default selection is
  live; live row carries `Published · Live`, no "current"/"selected"
  text; toolbar shows the badge for live and `Promote to live` for an
  older row; promote on candidate → `promoteCandidate`, on older →
  `promoteVersion(N)`; a 422 renders the gate name; success → `onPromoted`.
- pytest: `playbook_version` present in `GET /runs` and `GET /runs/{id}`.

## Verification

tsc, vitest, pytest green; `vite build` OK. Browser verification is
phase 7's (needs an owner token — see phase 1 summary).
