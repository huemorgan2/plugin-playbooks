# Phase 3 — ship

## Scope

Version, merge, push, publish. No code changes beyond the version stamps.

## Deliverables

- Bump **0.20.0 → 0.21.0** in all three stamps (see
  plugin-version-three-stamps): `pyproject.toml`, the in-code
  `PluginManifest` in `plugin_playbooks/__init__.py` (authoritative),
  `plugin_playbooks/luna-plugin.toml`.
- Commit on `014-failed-run-awareness` in the worktree; merge to `main`;
  push origin (gh auth switch to huemorgan2 first).
- Publish 0.21.0 to marketplaces.com.ai (publish-plugin skill; creds =
  workspace `.env` `LUNA_MP_TOKEN`; skill has stale paths — source the
  workspace .env directly).
- Master execution summary at the plan root.

## Verification criteria

- Full test suite green after the bump.
- `git log main` shows the merge; `git push` accepted.
- Marketplace index lists plugin-playbooks 0.21.0.
