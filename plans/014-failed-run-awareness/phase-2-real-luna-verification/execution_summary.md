# Phase 2 — real-Luna + dojo verification: execution summary

No production code changes (as planned). All five PHASE.md checks passed
against a real running Luna with a real Anthropic LLM.

## Setup as run

- Isolated `luna serve` from the `luna` checkout on port **8767** (8766 was
  held by another session's live QA server — left untouched), fresh sqlite
  DB in the session scratchpad, `env -u ANTHROPIC_API_KEY` so the real key
  from `luna/.env` is used.
- `LUNA_MANAGED_DIR` pointed at a scratch dir containing only the worktree
  `plugin_playbooks` package. Note learned here: the in-tree
  `luna/plugins/plugin_playbooks` does NOT load — for the old-schema boot
  the 0.16.0 package from `~/.luna/managed_plugins` was staged instead.
- Driver: `qa_014_drive.py` (saved in this folder), reusing
  `luna/scripts/qa_drive.py` helpers.

## Check results

1. **Migration — PASS.** Boot 1 with plugin 0.16.0 created the pre-014
   `playbooks` table (20 columns, no ack column). Boot 2 with the worktree
   package logged `playbooks: added failures_acked_version column to
   playbooks`; PRAGMA confirms `failures_acked_version INTEGER`. 0
   tracebacks in the server log.
2. **Failing playbook — PASS.** `qa-014-failing` created via
   `POST /api/p/plugin-playbooks/playbooks` (a `send_chat_message` step
   whose message template is `{{ 1 / 0 }}`: passes create validation,
   raises at render). Both API-started runs ended `failed`.
3. **Digest reaches the agent — PASS.** Fresh conversation, neutral opener
   "hi — anything I should know about?". The agent called
   `playbook_status`, then surfaced: "there's a playbook called
   qa-014-failing that's been failing on every run (4 out of 4)" and
   offered delete/disable vs dismiss — exactly the own-it-and-ask
   behavior the plan wanted, with no interruption channel involved.
4. **Ack flow — PASS.** Owner replied "known issue, just ignore it". Agent
   called `playbook_ack_failures`; DB ground truth
   `playbooks.failures_acked_version == 1`. Follow-up turn in a fresh
   conversation ("how are my playbooks doing?") listed the playbook via
   `playbook_list` without re-raising the failure digest.
5. **Transcript — saved** as `qa-transcript.log` in this folder (agent
   prose + tool calls per turn), driver as `qa_014_drive.py`.

## Deviations / environment drift found (not plugin bugs)

- `PUT /api/identity` no longer exists in current luna (405, GET only);
  the driver completes onboarding via qa_drive's direct DB write only.
- `GET /api/approvals?status=pending` returns a non-JSON body on this
  build, crashing qa_drive's auto-approver; the driver monkeypatches a
  safe `pending()`. Worth fixing qa_drive upstream some day (out of scope
  here — luna-service/luna are read-only by default).
- Port 8766 in AGENT-QA-TESTS.md was occupied by another session; used
  8767.

## Reassessment of remaining phases

No plan changes. Phase 3 proceeds as written: bump 0.21.0 in all three
stamps, commit, merge `014-failed-run-awareness` to main, push, publish
to marketplaces.com.ai.
