# Phase 0 — housekeeping

Scope: get the repo shippable before building.

1. Baseline: run the full pytest suite at 0.7.0 — must be green before any
   change.
2. Fix the three-stamps miss: `luna-plugin.toml` says 0.6.0 while the in-code
   manifest + pyproject say 0.7.0. Align toml to 0.7.0.
3. Commit, push (huemorgan2), publish 0.7.0 to the official marketplace
   (0.6.0/0.7.0 were pushed earlier but never published; marketplace is at
   0.5.1). Verify catalog latest_version == 0.7.0.
4. Sync QA Luna managed_plugins to 0.7.0 so later phases verify against the
   real current code.

Verification: pytest green · catalog 0.7.0 · QA Luna /api/plugins shows 0.7.0.
