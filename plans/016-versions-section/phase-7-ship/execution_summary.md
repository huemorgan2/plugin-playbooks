# Phase 7 — ship 0.28.0 — execution summary

**Published: plugin-playbooks 0.28.0** on marketplaces.com.ai (`official`),
zip sha256 `5a3c4bbcebe194cdff92e8106883732ff51a3dea4b2ae6a86e6adf9eaa91e3dd`.
Catalog `latest_version` verified as 0.28.0 right after upload.

## What shipped

Phases 3–6 of plans/016 in one release:

- Versions is a full-page section (`Versions · Settings` tabs): list on the
  right, selected version on the left, opens on live, highlighted row only,
  `Published · Live` / `Candidate` badges, toolbar
  `vN · date · Canvas | Code | Manifest | Tests | Runs · badge/Promote to live`,
  promote refusals shown inline (the silent-promote bug is gone).
- Specs (tests) belong to a version and are duplicated onto every new
  version; the specs gate applies to candidates AND restores/rollbacks.
- Settings is a full page with "Agent can trigger" and the new
  "Publish / Promote settings" switches (`require_specs`, `require_run`).

## Steps

1. Stamps 0.27.1 → 0.28.0 in `plugin_playbooks/__init__.py`
   (PluginManifest), `plugin_playbooks/luna-plugin.toml`, `pyproject.toml`.
   `ui-src/package.json` (0.3.1) is not a plugin stamp and was left alone.
2. `npm run build` in `ui-src` → `plugin_playbooks/ui/`
   (`assets/index-o_FQ7FlK.js`, `index-CsM3pyeX.css`), committed.
3. On the stamped tree: pytest **302 passed**, vitest **126 passed**,
   `tsc --noEmit` clean.
4. Packaged from a copy without `__pycache__`, `*.pyc`, `node_modules`,
   `ui-src`, `media` via `scripts/package_plugin.py` (26 files, one
   top-level `plugin_playbooks/`), published with `scripts/publish_plugin.sh`.
5. `/Users/roy/Documents/Luna/luna/plugin-set.toml` repinned to 0.28.0 +
   the sha256 above (uncommitted in the luna repo, as the 0.27.1 repin was).
6. Local QA Luna (`luna serve --port 8765`, `LUNA_DB_POOL=0`, Postgres on
   :5433): the managed copy `~/.luna/managed_plugins/plugin_playbooks` was
   replaced by the 0.28.0 package (0.27.1 copy backed up in the session
   scratchpad) and the server restarted. Verified by querying Postgres:
   `playbooks.publish_require_specs` / `publish_require_run` exist with
   default `true` (all existing rows `true`), `playbook_specs.playbook_version`
   exists (default 0), the legacy `ix_playbook_specs_playbook_name` index
   is gone and `ix_playbook_specs_playbook_version_name` is present. The
   local DB has no spec rows, so the backfill had nothing to move. The
   plugin loaded with only the pre-existing codegen-drift warnings; the
   served `ui/index.html` references the new bundle, which contains the
   "Publish / Promote settings", "Published · Live" and "Promote to live"
   strings.

## Not verified

- Browser walk-through of the Versions/Settings pages on the local Luna:
  every playbook route is behind `get_current_user` and no owner token is
  available in this session (see phase 1 summary). The pages were
  verified through the vitest component tests (VersionsTab, TestsTab,
  PublishSettings, VersionCanvas) instead. First real look is on the
  owner's agent after upgrading.

## Deviations from PHASE.md

- Step 6's "confirm the migration log lines": the plugin logger's INFO
  lines do not reach the server's stdout log (only WARNING+ appears), so
  the schema was confirmed by direct SQL instead of by log lines.

## Reassessment of remaining phases

None remaining — plans/016 is complete. Follow-ups outside the plan:
the owner must upgrade plugin-playbooks on their agent(s) to 0.28.0
(publishing does not auto-upgrade), and the luna repo's `plugin-set.toml`
repin is still an uncommitted local change.
