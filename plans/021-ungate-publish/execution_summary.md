# 021 — ungate publish: execution summary

Shipped as **plugin-playbooks 0.37.0** (2026-09-02).

## What landed

1. **Plans feature deleted end-to-end.** `plans.py`, `PlaybookPlan` /
   `PlaybookAppSetting` models, `PLAN_STATUSES`, the three plan tools
   (`playbook_plan_write/read/finish`), the plan gate in both publish paths,
   `plans_full_power` (and its card skip), the `/plans` +
   `/playbooks-settings` routes, the PlansTab UI, and every plan mention in
   prompts. Orphaned `playbook_plans` / `playbook_app_settings` tables stay
   on live tenants (unused).
2. **Explanation gate deleted.** `explanation` is optional on
   `playbook_publish` / `playbook_rollback`; the approval card still rises
   for EVERY agent publish — no full-power skip.
3. **Manifest drift gate deleted.** `_drift_check`, `skip_drift` /
   `forced`, and the whole `playbook_edit_force` tool are gone. The
   manifest is context, not law: the delegate still gets it in its prompt
   as helpful perspective, and `playbook_manifest_set` is now auto_approve
   so keeping it fresh is cheap.
4. **Owner UI never blocked on specs/test-run.** Promote click opens a
   confirm popover with green/red ✓/✗ bullets (tests state, run count)
   computed client-side from the versions list; the button reads "Publish"
   when all green, "Publish anyway" otherwise. Same for restore/rollback.
   Static validation and probes still 422 — the probes refusal names the
   broken tool and why.
5. **Agent gates kept:** candidate-exists, static validation, probes, and —
   per the kept per-playbook `publish_require_specs` / `publish_require_run`
   toggles — specs and test-run (toggle off = reported, never refused).
6. **Approval card gained ✓/✗ gate-status bullets** ("Checks" block) in
   owner words via `_gate_owner_line`.

## Versions / release mechanics

- Three stamps at **0.37.0** (`__init__.py` PluginManifest,
  `luna-plugin.toml`, `pyproject.toml`). 0.35.0/0.36.0 were consumed by a
  parallel session's plans-tab releases (see below).
- toml: 24 tools / 10 tables; `[[tools]]` blocks for edit_force + plan
  tools removed; manifest_set auto_approve/low.
- Tests: 332 pytest, 129 vitest, all green. `tests/test_plans.py` deleted;
  UI `PlansTab` + its tests deleted; VersionsTab tests rewritten for the
  confirm-dialog flow.
- UI rebuilt into `plugin_playbooks/ui/`.

## Supersede note (parallel-session collision)

While this plan was in flight, another session shipped **0.35.0
"plans/021: per-playbook Plans tab"** and **0.36.0 "plans/022: owner plan
controls"** — both entirely inside the plans feature this plan deletes.
Merged with `-s ours` (history preserved, tree superseded) and released as
0.37.0. Those sessions never committed plans/021 or plans/022 doc
directories, so this directory remains the canonical plan 021.
