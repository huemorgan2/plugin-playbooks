# 021 — Ungate publish: delete plans, drift gate, explanation gate; owner never blocked

Owner decisions (2026-09-02):

1. **Delete the plans feature entirely.** `plans.py`, `PlaybookPlan`/`PlaybookAppSetting`
   models, `PLAN_STATUSES`, the 3 plan tools (`playbook_plan_write/read/finish`), the
   plan gate in both publish paths, `plans_full_power` (and its auto-approve skip),
   `/plans` + `/playbooks-settings` routes, the Plans tab, all plan wording in prompts,
   db_tables entries. Orphaned tables on live tenants stay in place.
2. **Delete the explanation gate** (≥80 chars). `explanation` becomes optional on
   `playbook_publish`/`playbook_rollback`; the approval card stays for EVERY agent
   publish (full-power skip is gone with the feature).
3. **Delete the manifest drift gate**: `_drift_check` LLM call, `skip_drift`/`forced`/
   `why` in `_edit_impl`, and the whole `playbook_edit_force` tool. The manifest stays
   as a non-enforced perspective: the agent is told to read it before changing things
   and may update it freely (`playbook_manifest_set` becomes auto_approve). The
   delegate still gets the manifest in its prompt, framed as helpful context, not law.
4. **UI promote/rollback never blocks on specs or test-run.** The Promote click opens a
   confirm with green/red (✓/✗) status bullets — tests state, test-run state — and a
   "Publish anyway" style confirm. Static validation and probes still 422 (probes name
   the broken tool and why). Bullets are computed client-side from the versions list
   (specs cache + runs count).
5. **Agent gates that stay**: candidate-exists, static validation, probes, and — per
   playbook toggles `publish_require_specs`/`publish_require_run` (kept) — specs and
   test-run. Toggles off = reported, never refused (existing `require=False` semantics).
6. **Approval card gains ✓/✗ gate-status bullets** built from the gates list.
7. Version 0.37.0 (three stamps; 0.35.0-0.36.0 were taken by the superseded plans-tab releases), UI rebuild, tests, push, marketplace publish,
   QA-Luna verify.
