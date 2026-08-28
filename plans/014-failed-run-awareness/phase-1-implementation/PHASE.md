# Phase 1 — implementation + unit tests

## Scope

All code for plan 014, per the simplified plan:

1. `Playbook.failures_acked_version: int | None` — model column plus a
   `_COLUMN_MIGRATIONS` entry (`("playbooks", "failures_acked_version",
   "INTEGER")`) so existing installs get the ALTER.
2. Failing-playbooks digest in `PlaybooksPlugin.prompt_sections()`:
   one grouped query over `playbook_runs` scoped to the live version,
   `status='failed'`, unacked; renders counts, server-computed relative
   ages, last failed run_id, and the fixed ownership instructions.
3. New tool `playbook_ack_failures(name)` in `agent_tools.py` —
   auto-approve, low risk, NOT skill-gated (not added to the
   playbook-authoring SkillDef tool list).
4. Unit tests (new file `tests/test_failed_run_awareness.py`).

## Deliverables

- Code as above; no UI, no routes changes, no caching.
- Tests covering: version scoping, ack predicate, candidate-run
  exclusion, cancelled excluded, `live_version == 0` legacy rows,
  digest absent when clean, rendering (counts/ages/run_id/instructions),
  ack tool behavior, promote re-arms.

## Verification criteria

- Full suite green (baseline 189 + new tests), `PYTHONPATH=. uv run
  --extra dev pytest -q`.
- Grep confirms no tool-name collision for `playbook_ack_failures`
  across the other plugins in luna-plugins.

## Shipping note

Version bump and publish are deferred to phase 3 (batched — nothing
user-visible ships from the worktree until QA passes).
