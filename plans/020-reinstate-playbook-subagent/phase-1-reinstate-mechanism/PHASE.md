# Phase 1 — Reinstate the delegation mechanism (modernized)

## Baseline (2026-09-02)

- plugin-playbooks `b430e72`, 0.33.0. pytest **302 passed**, vitest 126, tsc
  clean. Delegation module absent since `fc2e017`.
- Recovered originals in scratchpad `old-subagent/` (delegation.py.old,
  card.py.old, models/routes/__init__/toml diffs, 3 test files).

## Scope

Reinstate `playbook_agent` + `playbook_agent_status` + the card plumbing,
adapted to the 0.33.0 world. No new prompt yet (phase 2), old card HTML kept
(phase 3 replaces it).

### `plugin_playbooks/delegation.py` (new file, from delegation.py.old)

- ToolDef modes → `["planning", "building"]` on both tools (old
  `fix_approve`/`fix_publish` states no longer exist — luna 098).
- `delegate_toolset`: authoring tools (incl. `playbook_preflight`) +
  `playbook_list`/`playbook_status` + tools referenced by the target
  playbook's definition, **plus** `playbook_plan_write`, `playbook_plan_read`,
  `playbook_plan_finish`; `send_chat_message` filtered out.
- `_drive_delegation` passes `conversation_state="building"` to
  `ctx.agent.run_turn` (publish-class tools declare
  `modes=["planning","building"]`; the headless turn must land in a real
  state).
- `_delegate_prompt` kept as the seam but content minimally updated to
  mention the plan-gate (write plan row before publish) so phase 1 is
  self-consistent; full 11-section rewrite is phase 2.
- Everything else preserved: capability token, `_LIVE_FEEDS`, `_EventFeed`
  duck-typing (`.result` or `.part`), phase inference, waiting-on-owner,
  `_aborted` budget handling, running/waiting status copy, orphan sweep.

### `models.py`

- `PlaybookDelegation` table restored verbatim (id, task, playbook, status,
  card_token, conversation_id, card_message_id, events JSONB, result,
  steps_used, started_at, finished_at).

### `routes.py`

- Unauthed `GET /delegations/{delegation_id}/card?token=` restored:
  `secrets.compare_digest`, single 404 for unknown-id and bad-token, result
  withheld while running, events tail 200, `ACAO:*` + `no-store`.

### `__init__.py`

- `_DELEGATION_SKILL_BODY` restored with plan-gate-aware wording where the
  old text referenced the pre-plan world; `playbook-delegation` SkillDef
  (tools: playbook_agent, playbook_agent_status, playbook_set_autonomy);
  authoring-skill description regains the "load playbook-delegation instead"
  steering; `DELEGATION_TOOLS` tuple + `_register_tool` gating; on_load
  registration + `sweep_orphaned_delegations` (never blocks load).

### `luna-plugin.toml`

- `db_tables` += `playbook_delegations`; tools 26→28, tables 11→12; the two
  `[[tools]]` entries restored.

### Tests

- `tests/test_delegation.py`, `tests/test_card_html.py`,
  `tests/test_card_route.py` adapted from the .old versions to the current
  tree (imports, plan tools in allowlist assertion, modes assertion updated
  to the new vocabulary).

## Verification

- Full pytest green (302 baseline + restored tests), `test_ops_exceptions.py`
  untouched and green.
- QA Luna (:8765): plugin loads, 28 tools registered, `playbook_delegations`
  table exists (direct SQL against localhost:5433, credentials masked).
- No version bump / no publish this phase (batching into 0.34.0 at phase 4).

## Non-goals

New delegate prompt (phase 2), args+ok/err in events (phase 2), card redesign
and luna-service proxy (phase 3), publish (phase 4).
