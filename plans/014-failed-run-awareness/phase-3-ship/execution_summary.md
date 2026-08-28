# Phase 3 — ship: execution summary

Shipped as **0.22.0** (not 0.21.0 as planned — see deviation).

## What happened

- Bumped the three stamps to 0.21.0 in the worktree, full suite green
  (206 passed), committed 7f45681 on `014-failed-run-awareness`.
- On returning to the main checkout, `origin/main` had moved: another
  session shipped **0.21.0 (plans/012 phase 3 — payload diet, 3d07f8a)**
  while this plan was in flight. Versions are immutable on the
  marketplace, so this plan rebumped to **0.22.0**.
- Merge `014-failed-run-awareness` → main (60f3428) was clean — git
  auto-merged our digest/ack additions into the payload-diet refactor of
  `__init__.py`. Verified our symbols survived and ran the full suite on
  the merged tree: **207 passed**.
- 0.22.0 stamp commit b99cc8d (amended to drop an accidentally staged
  worktree gitlink and another session's untracked plans/011 file);
  pushed as 6c0da35 (`3d07f8a..6c0da35`) with gh auth switched to
  huemorgan2.
- Packaged with `scripts/package_plugin.py`, published to
  marketplaces.com.ai `official` via `scripts/publish_plugin.sh`
  (workspace `.env` `LUNA_MP_TOKEN`; the skill doc's Documents/Luna
  paths and TOKEN2 name are stale). Catalog verified:
  `latest_version == 0.22.0`.

## Deviations from PHASE.md

- Version is 0.22.0, not 0.21.0 (collision with plans/012 phase 3 which
  landed on main mid-flight). No other deviations.

## Reassessment

Plan complete. Note for future plans: don't stamp the target version in
PLAN.md/PHASE.md — pick it at ship time against current main
(pull-main-before-changes applies to plugin repos too).
