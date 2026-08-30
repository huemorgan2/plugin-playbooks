# Phase 7 — ship 0.28.0

## Baseline (2026-08-30, after phase 6)

- plugin-playbooks `bd8f120`. pytest 302, vitest 126, tsc clean.
  Stamps at 0.27.1; marketplace latest 0.27.1; luna `plugin-set.toml`
  pinned 0.27.1 (uncommitted repin in the luna repo).

## Scope

Phases 3–6 go out as one user-visible release.

1. Stamps → 0.28.0: `plugin_playbooks/__init__.py` PluginManifest,
   `luna-plugin.toml`, `pyproject.toml` (+ `ui-src/package.json`).
2. `npm run build` in `ui-src` → committed dist in `plugin_playbooks/ui/`.
3. Full pytest + vitest + tsc on the stamped tree.
4. Package (copy without `__pycache__`, `*.pyc`, `node_modules`, `ui-src`,
   `media`) → `scripts/package_plugin.py` → publish to `official` with
   `LUNA_MP_TOKEN2` → verify catalog `latest_version == 0.28.0`.
5. Repin `/Users/roy/Documents/Luna/luna/plugin-set.toml` to 0.28.0.
6. Real-environment check on the local QA Luna (:8765): install the
   0.28.0 package as the managed copy, restart, confirm the migration log
   lines (`playbook_version` column, legacy index drop, spec backfill,
   `publish_require_specs` / `publish_require_run` columns) and probe
   `GET /playbooks/{name}` for the new fields. Browser verification of the
   Versions/Settings pages needs an owner token — if none is available,
   record that as the open item.
7. Commit + push; execution summary; remind the owner to upgrade the
   plugin on their agent.

## Verification

Catalog shows 0.28.0; local Luna loads 0.28.0 without errors; tests green.
