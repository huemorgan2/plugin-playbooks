# Phase 1 — implementation: execution summary

Commit: 80caaf9 on branch `014-failed-run-awareness` (worktree). No
version bump yet — batched to phase 3 per PHASE.md.

## What shipped

- `plugin_playbooks/models.py` — `Playbook.failures_acked_version:
  int | None` (version-scoped ack; NULL = never acked).
- `plugin_playbooks/__init__.py` —
  - `_COLUMN_MIGRATIONS` entry `("playbooks", "failures_acked_version",
    "INTEGER")` so existing installs get the additive ALTER on load;
  - module-level `_rel_age`, `failure_digest(session)`,
    `render_failure_section(digest)`;
  - `prompt_sections()` now runs the digest in its existing session
    (wrapped so a digest error can never take down the playbook-list
    section) and appends the rendered digest as a second section when
    non-empty.
- `plugin_playbooks/agent_tools.py` — `playbook_ack_failures(name)`:
  sets `failures_acked_version` to the effective live version.
  auto_approve / low risk / chat_only, NOT in the playbook-authoring
  SkillDef (must be callable the moment the owner says "ignore it").
- `tests/test_failed_run_awareness.py` — 17 tests.

## Digest semantics (as implemented)

- Scope: enabled playbooks; runs where `playbook_version ==
  coalesce(nullif(live_version,0), version)`; `failed` counted against
  `finished` (= failed + done; running/cancelled excluded from both).
- Hidden when `failures_acked_version == effective live version`;
  re-armed automatically by any promote (new live version).
- Candidate runs excluded by construction (their `playbook_version` is
  the candidate number). Verified in code that dry runs
  (`runner.dry_run`) and spec evaluations do not write `playbook_runs`
  rows.
- Detail per failing playbook: last failed run id + started_at, live
  version's `PlaybookVersion.created_at` (may be absent on legacy rows —
  rendered without the "promoted … ago" clause).
- Ages rendered server-side ("20 minutes ago"); instructions tell the
  agent to inspect via `playbook_status(run_id)`, raise it with the
  owner after finishing their request, offer fix/disable/dismiss, and
  never derail a muted/trigger turn.

## Verification

- Full suite in the worktree: `PYTHONPATH=. uv run --extra dev pytest -q`
  → **206 passed** (baseline 189 + 17 new), 0 failed.
- `playbook_ack_failures` name grepped across all plugins in
  luna-plugins — no collision.

## Deviations from PHASE.md

None.

## Reassessment of remaining phases

No plan changes. For phase 2 (real-Luna + dojo verification) note:
`prompt_sections()` digest needs a playbook whose live version has a
failed run — quickest honest path on QA Luna is a playbook with a step
that calls a nonexistent tool, run twice via `playbook_run` (or its
trigger), then a fresh chat turn to observe the section, then ack.
