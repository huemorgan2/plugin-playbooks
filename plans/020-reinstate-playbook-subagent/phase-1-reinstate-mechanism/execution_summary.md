# Phase 1 — execution summary

Commit `dd10ace` (pushed to origin main). No version bump / no publish —
0.34.0 ships once at phase 4, per PLAN.md's versioning note.

## What shipped

- `plugin_playbooks/delegation.py` — reinstated from the pre-fc2e017
  original, modernized:
  - ToolDef `modes=["planning","building"]` on both tools (the old
    `fix_approve`/`fix_publish` states no longer exist — luna 098).
  - `delegate_toolset` now also grants `playbook_plan_write` /
    `playbook_plan_read` / `playbook_plan_finish` (publish is plan-gated;
    the delegate writes the plan row itself). `send_chat_message` still
    filtered.
  - `_drive_delegation` passes `conversation_state="building"`
    unconditionally (a delegate spawned from a planning chat still builds;
    containment = explicit allowlist + machine-checked publish gates).
  - `_PHASE_BY_TOOL` maps the plan tools (plan_read → Understand,
    plan_write/plan_finish → Ship).
  - Interim `_delegate_prompt` gained plan-gate and "dry_run is simulated"
    rules; the full 11-section rewrite is phase 2.
- `plugin_playbooks/card.py` — restored verbatim (phase 3 redesigns it).
- `models.py` — `PlaybookDelegation` restored (12 columns, unchanged).
- `routes.py` — unauthed `GET /delegations/{id}/card?token=` restored
  (compare_digest, single 404 for unknown-id AND bad token, ACAO:*,
  no-store, live-feed-preferred, events tail 200).
- `__init__.py` — `_DELEGATION_SKILL_BODY` (updated for the plan-gate
  world, trimmed to 2,559 chars to stay under the small-skill cap),
  `playbook-delegation` SkillDef, authoring-skill description steers to
  delegation again, `DELEGATION_TOOLS` + skill-gated registration, on_load
  `build_delegation_tools` + `sweep_orphaned_delegations`.
- `luna-plugin.toml` — `playbook_delegations` in db_tables, tools 26→28,
  tables 11→12, two `[[tools]]` entries.
- Tests: `test_delegation.py`, `test_card_html.py`, `test_card_route.py`
  restored; two adaptations — the allowlist test asserts the plan tools,
  and `test_delegate_inherits_spawning_chat_state` became
  `test_delegate_always_runs_as_building`. `test_manifest_drift.py`'s
  `_code_tooldefs` now includes `build_delegation_tools` (its own comment
  already promised that).

## Verification

- Full pytest: **331 passed** (302 baseline + 29 restored), including
  `test_ops_exceptions.py` untouched and green.
- QA Luna on :8765 restarted with the synced managed copy: plugin loads
  clean (no playbooks errors in the log), UI route 200, card route returns
  a single 404 for an unknown id (no oracle), and the
  `playbook_delegations` table exists in Postgres with all 12 columns
  (checked by direct SQL).

## Surprises / learnings

- The skill-body length cap test (`< 2560`) bit three times while wording
  the plan-gate mention — the cap is the feature (small unlock), so the
  body was tightened rather than the cap moved.
- `luna-plugin.toml`'s drift comment still said the tool list is generated
  from "agent_tools.build_tools + delegation.build_delegation_tools" — the
  fc2e017 removal never updated it; the drift test just hadn't enforced
  the delegation half. Now it does.

## Reassessment of remaining phases

- No changes to phases 2–4. Confirmations that de-risk them: run_turn's
  full kwargs surface is intact, post_chat_card exists, the card route
  behaves — so phase 3's luna-service pass-through is genuinely the only
  missing link for hosted cards. Phase 2 should also delete the interim
  plan-gate lines from `_delegate_prompt` when replacing it wholesale.
