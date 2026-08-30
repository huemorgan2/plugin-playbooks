# Phase 2 — version API — execution summary

**Shipped in the repo, not yet released:** batched into 0.28.0 (phase 7) —
nothing here is user-visible on its own. Stamps stay at 0.27.1.

## What changed

- `plugin_playbooks/routes.py`
  - new `GET /playbooks/{name}/versions/{n}` (`get_version`) →
    `{version, definition, code, manifest, author, message, created_at,
    promoted_from, live, candidate, runs}`; stored row served as-is, the
    legacy live version without a row is served from the `Playbook` row,
    otherwise 404. `runs` counts runs of that version only.
  - `GET /playbooks/{name}/runs` takes `?version=N`
    (filter on `PlaybookRun.playbook_version`); default unchanged.
- `ui-src/src/playbooks/types.ts`: `VersionDetail`.
- `ui-src/src/playbooks/api.ts`: `getVersion(name, n)`,
  `listRuns(name, version?)`.
- `tests/test_version_routes.py`: 4 tests (stored row incl. per-version run
  count and `live`/`promoted_from`; legacy live without a row, cross-checked
  against `list_versions`; 404s for unknown version and playbook; runs
  filter with/without `version`).

## Verification

- pytest **283 passed** (279 + 4). vitest 111 passed; `tsc --noEmit` clean.
- No real-environment step (as planned: the route gets exercised by the
  phase-4 UI and verified in the browser in phase 7).

## Deviations from PHASE.md

None.

## Reassessment of remaining phases

- Phase 3 (extract `VersionCanvas` + read-only code view): unchanged. Its
  input is exactly `VersionDetail.definition` / `.code`, so the extracted
  component should take a `PlaybookDef` + `code` rather than the whole
  playbook object — decided now so phase 4 needs no refactor.
- Phase 4: unchanged; uses `getVersion` and `listRuns(name, n)`.
- Phases 5–7: unchanged.
