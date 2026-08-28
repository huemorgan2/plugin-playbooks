# Phase 1 — delegation core

Scope: the `PlaybookDelegation` table, the `playbook_agent` /
`playbook_agent_status` tools, the background delegate loop through
`ctx.agent.run_turn`, toolset scoping, step budget, and the event feed.
No UI in this phase (the card is phase 2); the tools are already wired
to everything the card will read.

## Deliverables

- `plugin_playbooks/models.py`: `PlaybookDelegation` — id, task (Text),
  playbook (String, may be empty), status
  running|done|failed|needs_owner, card_token (String(64), random,
  for the phase-2 unauthed card route), conversation_id,
  card_message_id (filled in phase 2), events (JSONB list), result
  (Text), steps_used (Integer), started_at, finished_at. Registered on
  the plugin Base → auto-created by the existing on_load DDL loop.
- `plugin_playbooks/delegation.py` (new):
  - `build_delegation_tools(ctx, session_factory)` → [(ToolDef,
    handler)] for `playbook_agent(task, playbook="", wait_seconds=25)`
    and `playbook_agent_status(delegation_id)`.
  - Delegate toolset: the authoring tools
    (`PlaybooksPlugin.AUTHORING_TOOLS`) + `playbook_list` +
    `playbook_status` + the tools referenced by the target playbook's
    `tool_call` steps (from its live definition). Never
    `send_chat_message` (the card is the surface), never `playbook_run`
    (chat_only already excludes it).
  - The loop: one `ctx.agent.run_turn(prompt, tools=...,
    max_turns=40, timeout_s=900, memory_write=False,
    conversation_id=<origin>)` driven inside an
    `asyncio.create_task` held in a module-level set (create_task alone
    is GC-bait — same pattern as runner.py). Prompt = the
    playbook-authoring skill body + the task + the target playbook's
    manifest + a delegate framing (work then STOP; final text = report).
  - Events: `event_stream_handler` maps pydantic-ai events to
    `{ts, phase, kind: tool|thought, label, detail, ms}` rows appended
    to `PlaybookDelegation.events` (throttled DB flush, ≤1 write/s).
    Phase inferred server-side from tool name (Understand/Change/
    Prove/Ship vocabulary per the plan); the model never emits codes.
  - Terminal mapping: normal return → done (result = final text);
    `{"_aborted": ...}` (budget/timeout breach) → needs_owner with a
    where-it-stopped summary; exception → failed.
  - Async contract: wait up to `wait_seconds` (default 25, max 90) then
    return `{delegation_id, status}`; the tool result instructs the
    main agent to tell the owner the card tracks progress and END its
    turn.
- `plugin_playbooks/__init__.py`: new `DELEGATION_TOOLS` tuple gated by
  a new lightweight SkillDef `playbook-delegation` (~1KB body: when to
  delegate, how to phrase the task, the end-turn rule). Registered
  skill_gated=True via the existing `_register_tool` path.
- Feature-detect older cores: `run_turn` called with the 049 kwargs in
  a try/except TypeError that degrades to a plain call (no budget) and
  records `budget_unenforced` in events.

## Verification

- Unit tests (`tests/test_delegation.py`) with a scripted fake
  `ctx.agent` (records kwargs, returns canned results): tool creates a
  row; fast path returns done inline; slow path returns running then
  status polls to done; `_aborted` → needs_owner; exception → failed;
  toolset passed to run_turn = authoring + referenced tools, no
  send_chat_message; max_turns=40 passed; events recorded with inferred
  phases; TypeError fallback path.
- Contract test: `len(json.dumps(tool_result)) < 1024` for the running
  and done shapes (main-turn payload stays ~1KB).
- Full pytest suite green (baseline recorded first).
- Real-Luna check deferred to phase 2 close (tools + card verified
  together on QA Luna); this phase ships no user-visible surface alone.

## Ship

Version 0.24.0 (three stamps: PluginManifest, luna-plugin.toml,
pyproject.toml) — committed on the worktree branch; push + marketplace
publish happen at plan close per the owner's merge-at-end instruction.
