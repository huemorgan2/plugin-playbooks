# Phase 7 — cleanup & hardening

Last phase of plans/002. Ships as **0.14.0** if any shipped-code change lands,
otherwise docs/tests-only touches ride the next release.

## Scope (PLAN.md + reassessments from phases 0–6)

1. **Remove deprecated YAML input** from `playbook_propose` / `playbook_edit`
   — code is the only authoring surface. Codegen backfill for any stored
   playbook still without `code` (eager path; verify none remain on QA).
2. **Dojo manifest-refusal scenario**: agent asked to change a playbook
   against its manifest must refuse or ask — the gates-beat-prose proof.
   Run against QA Luna :8766 with DB-probe verification per turn.
3. **Step-output normalization**: tool results in step outputs normalized to
   dicts (learning from phase 4 — spec `tool_calls` assertions saw mixed
   shapes).
4. **Docs**: README tool table (current names + skill-gated ones), manifest
   conventions, spec cookbook (stubs / expect blocks / failure examples).
5. **QA leftovers prune** (on QA Luna): delete `qa-p6-glow`, the
   "(edited during phase-6 browser QA)" manifest line on `qa-code-hello`,
   phase0/phase2 test playbooks; delete the scratch Chrome profile.

## Verification

- Full pytest suite green; vitest green if UI touched.
- Grep proves no YAML-input path remains in tools; propose/edit reject a
  `yaml=` argument with a steering hint.
- Dojo scenario transcript saved under this folder.
- README renders with the current tool table.

## Non-goals

- No new UI work; no schema changes; luna core stays untouched (0.34.020
  ProbeDef commit remains local pending owner decision).
