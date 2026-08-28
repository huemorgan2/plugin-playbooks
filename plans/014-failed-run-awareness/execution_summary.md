# Plan 014 — failed-run awareness: master execution summary

Shipped as **plugin-playbooks 0.21.0**. The agent now learns about
playbook run failures ambiently — through its per-turn prompt sections,
never through an interrupting message — scoped to runs of the playbook's
current live version ("since the last change the owner made"), and can
own the conversation: inspect, raise it with the owner after finishing
their request, then fix / disable / dismiss.

## What shipped

- `Playbook.failures_acked_version` (int, nullable) + additive migration.
- `failure_digest()` + `render_failure_section()` in
  `plugin_playbooks/__init__.py`; `prompt_sections()` appends the digest
  as a second section when any enabled playbook's live version has
  unacked failed runs. Counts auto-reset on promote (runs are keyed by
  `playbook_version`); ack is version-scoped so promote re-arms it and
  "ignore it" never re-nags.
- `playbook_ack_failures(name)` tool (auto-approve, chat-only).
- 17 unit tests (206 total green).

## Phases

- **Phase 0 — baseline**: 189 tests green pre-change; worktree on
  `014-failed-run-awareness` off main @ cd7a09f.
- **Phase 1 — implementation** (commit 80caaf9): model column, migration,
  digest + rendering, ack tool, tests. 206 passed.
- **Phase 2 — real-Luna verification**: isolated QA Luna (port 8767, real
  Anthropic LLM). Migration verified against a real 0.16.0 database
  (ALTER logged, PRAGMA confirmed). Behavioral run: agent surfaced
  "qa-014-failing … failing on every run (4 out of 4)" on a neutral
  opener, called `playbook_status` to inspect, acked via
  `playbook_ack_failures` when the owner said "ignore it"
  (DB: `failures_acked_version == 1`), and did not re-raise afterward.
  5/5 checks; transcript in phase-2 folder.
- **Phase 3 — ship**: 0.21.0 in all three stamps, merged to main, pushed,
  published to marketplaces.com.ai.
