# Phase 2 — real-Luna + dojo verification

## Scope

No production code changes expected. Prove the phase-1 build on a real
running Luna (unit tests are not verification — see
verify-plugins-on-real-luna): plugin load, schema migration, the digest
reaching a real agent, and real agent behavior (raise → owner decides →
ack), driven over the API like the dojo tests do.

## Setup

- Isolated QA server from the `luna` checkout (`.env` provides
  `LUNA_ANTHROPIC_API_KEY`; launch with `env -u ANTHROPIC_API_KEY` so the
  Claude shell's proxy key can't leak in), port 8766, fresh sqlite DB in
  the session scratchpad, scratch `LUNA_MANAGED_DIR`.
- Worktree `plugin_playbooks` package copied into the scratch managed dir
  (managed overrides the in-tree copy).

## Checks

1. **Migration**: boot once with the OLD in-tree plugin (empty managed
   dir) to create the pre-014 schema, kill, boot again with the worktree
   package — expect the `_ensure_columns` log line adding
   `failures_acked_version` and a clean load.
2. **Failing playbook**: create `qa-014-failing` via the plugin API (a
   `send_chat_message` step whose template raises at render), run it
   twice via `POST /playbooks/{name}/runs`, confirm both runs end
   `failed`.
3. **Digest reaches the agent**: fresh conversation, neutral opener
   ("hi — anything I should know about?"); the agent must surface the
   failing playbook and ask what to do.
4. **Ack flow**: owner replies "ignore it"; the agent should call
   `playbook_ack_failures`. Ground truth in DB:
   `playbooks.failures_acked_version == 1`. A following turn must not
   re-raise the failure.
5. QA transcript (agent prose + tool calls per turn) saved into this
   phase folder.

## Verification criteria

All five checks pass; any agent-behavior flake is retried once
(qa-gemini-style DB-probe + retry is Gemini-specific; this server runs
Anthropic — still allow one retry before calling it a failure).
