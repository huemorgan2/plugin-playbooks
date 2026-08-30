# Phase 4 — the Versions tab — execution summary

**In the repo, unreleased** (ships in 0.28.0 with phases 5–6). Stamps stay at
0.27.1; the committed `plugin_playbooks/ui/` dist is still 0.27.1 (phase 7
rebuilds it). This is the user-visible rework from Part B of the plan.

## What changed

### UI (`ui-src/src/playbooks/`)

- `PlaybookEditor.tsx` (888 → 473 lines, rewritten). Non-draft playbooks
  now have two top-level tabs, **Versions · Settings**, both normal
  `TabBtn`s with icon + word; Versions is the default. Removed: the
  icon-only History/Settings toggles, the header `Promote candidate vN`
  button, the side `VersionsPanel`, the side `SettingsPanel`, the
  live/candidate canvas switch and all `canvasSource` / `candidateDef` /
  `candidateCode` plumbing, the editor's run-attach and run-polling
  effects. Kept: the live-patch queue (500 ms stagger, replay on mount);
  for playbooks each applied patch is forwarded to `VersionsTab` as
  `patch={seq, evt}`; a `replace` patch calls `loadData()`, which bumps
  `refreshKey` so the Versions tab re-lists and re-fetches. Drafts are
  unchanged (Canvas · Code tabs, promote-draft button, glow on the editor's
  own canvas). `promoteRefusalMessage` is re-exported from `./VersionsTab`
  so existing imports keep working.
- `SettingsTab` (exported from `PlaybookEditor.tsx`) — full-page settings
  with the "Agent can trigger" section; accepts `children` so phase 6 can
  append "Publish / Promote settings" without touching the layout.
- `VersionsTab.tsx` (new, 481 lines). Right pane: version list, newest
  first; rows are buttons with `aria-current` and highlight classes
  (`bg-luna-600/20 border-luna-400 text-luna-200` + left accent) — no
  "selected" word anywhere; badge top-right: `Published · Live` (emerald)
  or `Candidate` (violet); author (`agent` / `you`), time-ago with absolute
  `title`, run count, `← promoted from vX`. Opens on the live version.
  Left pane toolbar: `vN` large bold · created date · segmented
  `Canvas | Code | Manifest | Tests | Runs` · right slot = `Published · Live`
  badge for live, otherwise **`Promote to live`** (Rocket). Promote:
  candidate row → `promoteCandidate`, any other → `promoteVersion(N)`;
  a 422 renders the gate message in a banner under the toolbar; success
  selects the new live and calls `onPromoted(n)`. Views: Canvas
  (`VersionCanvas` + `StepDetailPanel`), Code (`CodeView`), Manifest
  (`ManifestTab` for live — editable; read-only `<pre>` snapshot otherwise),
  Tests (`TestsTab`, still unversioned until phase 5), Runs (`RunsTab
  version={N}`). Live runs: subscribes to `playbook.run.started/completed`,
  selects the run's version and overlays it; polls a running run every
  1.4 s; "show on canvas" from Runs does the same for a past run. Agent
  patches are applied to the candidate (or live when there is no
  candidate) with node glow.
- `VersionCanvas.tsx` — `glow?: Map<string, number>` prop (glowSeq per node).
- `RunsTab.tsx` — `version?: number` prop → `listRuns(name, version)`;
  empty text "No runs of vN yet".
- `StepDetailPanel.tsx` (new) — `StepDetailPanel`, `execRowsForStep` moved
  out of the editor (avoids a circular import with `VersionsTab`).
- `editorBits.tsx` (new) — `TabBtn`, `timeAgo` moved out of the editor.
- `types.ts` — `playbook_version?: number` on `PlaybookRunSummary`.

### Backend

- `routes.py`: `list_runs` and `get_run` responses now carry
  `playbook_version`, so the UI can select the right version when a run
  starts or is opened. (`?version=N` filtering and `GET /versions/{n}` are
  from phase 2.)

### Tests

- `ui-src/src/playbooks/__tests__/VersionsTab.test.tsx` — 6 tests with a
  mocked `playbooksApi` and stubbed siblings (Manifest/Tests/Runs tabs,
  events): default selection is live + highlighted + `Published · Live`
  present, no "current"/"selected" text; older row → `Promote to live` →
  `promoteVersion('greeter', 1)` → `onPromoted(1)`; candidate row →
  `promoteCandidate`; 422 → `promote-error` banner names the gate
  message; view switch renders Code / Manifest (editable for live,
  snapshot for older) / Tests / Runs; `promoteRefusalMessage` precedence.
- `tests/test_version_routes.py::test_runs_carry_their_version` —
  `playbook_version` on `GET /runs` and `GET /runs/{id}`.

## Verification

- `tsc --noEmit` clean.
- vitest **121 passed** (115 + 6).
- pytest **284 passed** (283 + 1).
- `vite build` to a scratch dir succeeds (498 kB JS, unchanged order of
  magnitude).
- No browser check yet: the built dist is not committed until phase 7, and
  the local QA Luna still needs an owner token (phase 1 summary). Phase 7
  drives the real UI.

## Deviations from PHASE.md

- `SettingsTab` takes `children` (not in the phase doc) — a cheap seam for
  phase 6.
- `promoteRefusalMessage` moved into `VersionsTab.tsx` and is re-exported
  from the editor rather than left in place — the editor no longer
  promotes anything itself.

## Surprises / learnings

- The Write tool refuses to overwrite a file that was changed by a script
  since it was last read; re-read then write. `vite build` run from the
  wrong cwd (the shell resets to `docs/`) fails with an opaque stack —
  always `cd` to `ui-src` first.

## Reassessment of remaining phases

- Phase 5 (versioned specs): the UI seam is ready — `VersionsTab` renders
  `<TestsTab name={name} />` inside the `tests` view and already knows
  `detail.version`; phase 5 adds a `version` prop to `TestsTab` and
  `?version=N` to `listSpecs`/`runSpecs`, nothing else in the tab changes.
  Backend scope unchanged (column, unique index, backfill, single
  `mint_version()` helper that duplicates specs, gates evaluate the target
  version's specs).
- Phase 6 (publish settings): render the two switches as `children` of
  `SettingsTab`; the editor already loads the playbook row in `loadData`,
  so the flags can ride on `playbooksApi.get` and a `PATCH
  /playbooks/{name}/publish-settings`.
- Phase 7 unchanged: three stamps → 0.28.0, `npm run build` into
  `plugin_playbooks/ui/`, publish, repin, browser verification with an
  owner token.
