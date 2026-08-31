# Phase 3 — execution summary

## What shipped (0.30.0)

`plugin_playbooks/agent_tools.py`:

- `playbook_edit_force`, `playbook_manifest_set`, `playbook_spec_delete`,
  `playbook_set_autonomy` gained an optional `why` argument, FIRST in each
  schema so the legacy `prompt_always` approval card leads with plain
  language instead of raw arguments (shared `_WHY_PROP` description: "one
  or two plain sentences FOR THE OWNER"). Tool descriptions steer the agent
  to always provide it. Where a version row is minted (`edit_force`,
  `manifest_set`) the why is appended to the version-history message.
  Judgment call recorded in PHASE.md: all four STAY `prompt_always` — their
  cards are small and why-first makes them readable; phase-1-style
  handler-side conversion wasn't worth the lock restructuring here.

`plugin_playbooks/__init__.py` (`_OPS_MODE_SECTIONS`):

- `fix_approve` and `fix_publish` sections now tell the agent the owner is
  not an engineer: proposals, `why` arguments, and the publish
  `explanation` must say in everyday language what went wrong and what the
  fix does, tied back to the triggering failure; technical detail belongs
  in the collapsed card section. `fix_publish` adds the aggregation rule:
  one fix, one publish, ONE approval card — batch related edits into the
  candidate instead of asking per edit. Stage flow unchanged.

`plugin_playbooks/luna-plugin.toml`:

- Regenerated from code. It had been frozen at the 0.26 extraction: 12
  tools / 9 tables declared vs 25 / 10 real (`playbook_fix_proposals`
  missing from `db_tables`), and `playbook_set_autonomy` declared policy
  "ask", which isn't even a valid enum value. Now: 25 `[[tools]]` entries
  with the real policies/risk levels, 10 tables, `[requires]` 25/10,
  version 0.30.0, readme's stale "YAML/nine tables" wording fixed.

Versions: 0.30.0 in all three stamps (in-code `PluginManifest`,
`luna-plugin.toml`, `pyproject.toml`).

Tests:

- New `tests/test_manifest_drift.py` (4 tests): toml tools =
  `build_tools` + `build_delegation_tools` ToolDefs exactly (names,
  policies, risk levels, count); toml `db_tables` = `Base.metadata` tables;
  the three version stamps agree; the four `prompt_always` tools lead with
  an optional `why`.
- `tests/test_manifest.py` no longer freezes counts/table names/policies
  (that freeze is HOW the toml drifted) — it keeps identity, internal
  consistency, no-core-imports, and prebuilt-UI checks.

## Verification

Full plugin suite: **319 passed** (316 after phase 2, +4 new, −1 removed
frozen test), zero failures. Live E2E happens in master plan phase 4.

## Deviations from PHASE.md

None.

## Surprises / learnings

- The stub `ToolDef` in `tests/conftest.py` stores only the kwargs actually
  passed — the drift test reads policy/risk via `getattr` with the real
  SDK defaults ("auto_approve"/"low").
- The drift had a guard all along — `test_manifest.py` — but it pinned the
  toml to itself, not to the code, so it enforced the stale copy.

## Reassessment of remaining phases

Plan 018 is code-complete. Remaining: ship 0.30.0 (push huemorgan2 +
marketplace publish), then the master plan's phase 4 live E2E (failing
playbook → readable proposal card → approve → fix → explained publish card
→ approve → live), with screenshots into the master execution summary.
