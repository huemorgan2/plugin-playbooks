# Phase 3 — remaining gated mutations + hygiene + 0.30.0

## Scope

1. **`why` on the remaining `prompt_always` mutations** —
   `playbook_edit_force`, `playbook_manifest_set`, `playbook_spec_delete`,
   `playbook_set_autonomy` gain an optional-but-steered `why` argument,
   FIRST in the schema properties so the legacy approval card leads with
   plain language instead of raw args. Handlers accept it; where a version
   row is minted (`edit_force`, `manifest_set`) the why lands in the
   version-history message. Judgment call taken: all four STAY
   `prompt_always` — their cards are small and the why-first ordering makes
   them readable; handler-side conversion (phase-1 style) is not worth the
   lock restructuring for these low-traffic tools.
2. **Ops prompt sections** (`_OPS_MODE_SECTIONS` in `__init__.py`) — the
   fix modes now require the publish `explanation` to be written for the
   owner and to tie back to the failure that triggered the fix. Stage flow
   (identify → fix_approve → fix_publish) unchanged.
3. **Manifest drift cleanup** — `plugin_playbooks/luna-plugin.toml` was
   frozen at the 0.26 extraction: 12 tools / 9 tables declared vs 23 tools /
   10 tables real (`playbook_fix_proposals` missing), and
   `playbook_set_autonomy` declared a policy value ("ask") that isn't even
   the enum. Regenerate the `[[tools]]` list and `db_tables` from code;
   `[requires]` counts updated.
4. **0.30.0** across all three stamps: in-code `PluginManifest`
   (`__init__.py`), `luna-plugin.toml`, `pyproject.toml`.

## Verification

- New drift test: toml `[[tools]]` names/policies/risk and `db_tables`
  match `build_tools` defs and `Base.metadata` exactly — the freeze can't
  recur.
- Schema test: `why` present and FIRST in the four tools' properties, not
  required.
- Full suite green (316 after phase 2).

## Ship

0.30.0: commit, push (huemorgan2), publish to marketplaces.com.ai.
Live E2E happens in master plan phase 4.
