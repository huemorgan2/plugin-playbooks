# 029 — playbook_watch: execution summary

**Status: SHIPPED** — plugin-playbooks 0.46.0, fleet image 0.92.029 (2026-09-04).

## What shipped

One-shot "wake me when this playbook's next run finishes" for agents, any
trigger (webhook, scheduler, another conversation's agent, manual).

- `PlaybookWatch` table (`playbook_watches`): durable one-shot rows —
  playbook FK (CASCADE), watcher `conversation_id` stamped at watch time
  (017 rule: origin stamped at block time, never reconstructed), optional
  note, 7-day `expires_at`, `consumed_at` claim column.
- `RunCompletionWake._watch_pass` runs first in `_deliver_inner` for every
  real completion (test/subtask runs already filtered in `_on_completed`,
  so watches stay armed through those). Claim-first exactly-once:
  `UPDATE … WHERE consumed_at IS NULL`, rowcount-gated — concurrent
  completions deliver once. Expired watches reaped in-pass.
- New tools `playbook_watch` / `playbook_watch_cancel` (building mode
  only). Requires a wake-capable core (`send_muted_message` feature-detect)
  — otherwise the tool errors and steers to polling. Re-watch refreshes the
  existing row instead of duplicating. Reply promises WOKEN + end-turn +
  one-shot + 7-day expiry. Description deliberately contains no
  adapter-playbook steering (Roy's constraint): "use only for playbooks
  that already exist and are run by something else."

## Double-trigger audit (017 standing rule)

Per (run, watcher-conversation) at most ONE moment. The watch is consumed
*silently* (no moment) when another path already delivers to the same
conversation:

| Overlap | Owner path | Watch behavior |
|---|---|---|
| watcher == launcher with `wake_on_complete` | 028 launcher moment ("you started earlier") | consumed silently |
| agent-triggered run, watcher == that conversation | inline tool result | consumed silently |
| failed background run, watcher == ops chat | FixProposalService failure moment | consumed silently |
| everything else | watch moment ("Watched playbook finished: …") | delivered, then consumed |

Failure moments to non-ops watchers include the error + honest-report
steering. Wake caps: `tools="all"`, max_turns 12, token_budget 200k,
timeout 900s (028 values).

## Tests

11 new in `tests/test_playbook_watch.py` — tool contract (stamp, refresh,
cancel), single delivery + second-completion silence, all three
silent-consume rules, non-ops failure moment, expired reap, concurrent
claim, test/subtask immunity. Full suite 403 passed.

## QA E2E (real Luna, port 8766, managed_plugins 0.46.0)

- Conv A agent called `playbook_watch` on `qa-code-hello` (note "watch QA
  029"), ended its turn. DB: 1 watch row.
- Conv B agent ran the playbook via `playbook_run`.
- Conv A received exactly ONE "Watched playbook finished: qa-code-hello"
  moment (note echoed) and reacted with a correct report. Conv B got the
  inline result only — no watch moment. DB: `consumed_at` set.

## Rollout

- Repo: 32ff0dc pushed (huemorgan2/plugin-playbooks).
- Marketplace: 0.46.0 published, sha256 `dea6b0dd…0ec419` (27-file
  artifact matching the 0.45.1 namelist exactly).
- Fleet: pinned 0.46.0 → image build GH run 33916859316 (luna main
  bc9dd4a, 0.92.029) → promoted 36 machines, 0 errors, 0.92.028 deleted →
  verify: 21 stopped + 15 started all 0.92.029.
- Scanny: `/api/plugins` shows plugin-playbooks 0.46.0 (chat-ui 0.30.2
  intact).

## Notes

- Fleet pin history: image previously pinned 0.45.0 while marketplace had
  0.45.1; 0.46.0 supersedes both.
- Version stamps bumped in all three places (toml, pyproject, in-code
  manifest); the two new tools are building-only (kept out of the pinned
  planning-tool set in test_mode_declarations).
