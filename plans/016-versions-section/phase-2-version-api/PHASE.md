# Phase 2 — version API

## Baseline (2026-08-30, after phase 1)

- plugin-playbooks `f61ed89` (0.27.1 published). pytest 279 passed, vitest 111 passed.

## Scope

Backend only, no schema, nothing user-visible — ships with 0.28.0 (phase 7).

1. **New** `GET /playbooks/{name}/versions/{n}` → `{version, definition,
   code, manifest, author, message, created_at, promoted_from, live,
   candidate, runs}`. A stored `PlaybookVersion` row is served as-is; the
   legacy case (live version with no row — the entry `list_versions`
   synthesizes) is served from the `Playbook` row itself; any other missing
   version → 404.
2. `GET /playbooks/{name}/runs?version=N` filters on
   `PlaybookRun.playbook_version`. No `version` → unchanged (all runs).
3. `list_versions` unchanged (entries already carry `live` / `candidate`;
   the phase-1 idea of adding a top-level `live_version` is dropped — the
   list is an array, and the tab can pick the entry with `live: true`).
4. `?version=N` on specs is **not** here — it lands with versioned specs in
   phase 5.

## Tests (pytest, `tests/test_version_routes.py`)

- stored row → 200 with the row's definition/code/manifest, `live`
  correct, `runs` counted for that version only;
- legacy live version without a row → 200 served from `Playbook`;
- unknown version → 404; unknown playbook → 404;
- `runs?version=1` returns only v1 runs; without the filter, all.

## Verification

Full pytest green. No real-environment step: the route is exercised by the
phase-4 UI; browser verification happens in phase 7 with an owner token
(precondition recorded in phase 1's reassessment).
