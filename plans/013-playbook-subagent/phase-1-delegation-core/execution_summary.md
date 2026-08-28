# Phase 1 — execution summary

Shipped as version 0.24.0, commit 02e07fd on branch `013-playbook-subagent`
(worktree). Not yet pushed/published — per the owner's instruction the merge
to main, push, and marketplace publish happen at plan close.

## What shipped

- `plugin_playbooks/models.py`: `PlaybookDelegation` table
  (`playbook_delegations`) — task, playbook, status
  running|done|failed|needs_owner, card_token (random urlsafe secret for the
  phase-2 unauthed card route), conversation_id, card_message_id (phase 2),
  events (JSONB), result, steps_used, started_at/finished_at. Auto-created
  by the existing on_load DDL loop; added to luna-plugin.toml db_tables
  (now 9 tables).
- `plugin_playbooks/delegation.py` (new, ~470 lines): the whole core.
  - `build_delegation_tools(ctx, session_factory, authoring_tools)` →
    `playbook_agent(task, playbook="", wait_seconds=25)` (chat_only,
    auto_approve, medium risk) and `playbook_agent_status(delegation_id)`.
  - Delegate toolset: AUTHORING_TOOLS + playbook_list + playbook_status +
    tools referenced by the target playbook's tool_call steps (recursive
    walk over then/else/body/branches). `send_chat_message` always excluded.
  - One contained `ctx.agent.run_turn(prompt, tools=<allowlist>,
    max_turns=40, timeout_s=900, memory_write=False, memory_read=False,
    conversation_id=<origin>, event_stream_handler=...)` inside an
    asyncio task held in a module dict (`_TASKS`, GC protection + wait
    handle). TypeError fallback for pre-049 cores runs uncontained and
    records a "budget unenforced" event on the card feed.
  - Event feed (`_EventFeed`): duck-typed pydantic-ai events (matched by
    class NAME — no pydantic_ai import; the plugin stays SDK-only) →
    `{ts, phase, kind: tool|thought, label, detail, ms}`. Call + result
    collapse into one line per tool call with duration. Phase inferred
    server-side from the tool name (Understand/Change/Prove/Ship — owner
    words, model never emits codes). DB flush throttled to 1/s; a live
    in-process registry (`_LIVE_FEEDS`) exists for the phase-2 card route
    to read fresher-than-DB state.
  - Terminal mapping: normal return → done (final text = report, capped
    800 chars in tool payloads); `{"_aborted": ...}` → needs_owner with a
    where-it-stopped sentence; `{"error": ...}` or exception → failed.
  - `sweep_orphaned_delegations` on load: rows left "running" by a dead
    process → failed with a restart notice (same convergence rule as the
    runner's orphan sweep).
- `plugin_playbooks/__init__.py`: new `playbook-delegation` SkillDef
  (~1.4KB body: when to delegate vs. inline, task phrasing, the
  end-your-turn rule) gating `DELEGATION_TOOLS` via the existing
  `_register_tool` path; on_load registers the tools and runs the sweep
  (non-blocking on failure).
- `luna-plugin.toml`: 12 tools, 9 tables; readme count fixed.
- Three version stamps bumped 0.23.0 → 0.24.0 (manifest, luna-plugin.toml,
  pyproject.toml).

## Verified

- `tests/test_delegation.py` — 13 tests, all passed on first run: fast path
  done-inline; slow path running → status polls done; _aborted →
  needs_owner (with last-tool context); error/crash → failed; toolset
  includes nested referenced tools and never send_chat_message; 049 kwargs
  (max_turns=40, timeout_s=900, memory_write=False) asserted on the fake
  agent; event phases inferred correctly with one-line-per-call collapse;
  TypeError fallback; running payload <1KB and done payload <1.2KB;
  unknown playbook clean error with no row; orphan sweep; skill/tool
  wiring (chat_only, gated set, skill body <2KB).
- Full suite: 224 passed, 0 failed (baseline before phase: 211 passed).
  The only failures during the phase were the two manifest pin tests,
  updated intentionally for the new table/tools.
- Real-Luna check deferred to phase 2 close as planned (no user-visible
  surface ships alone in this phase).

## Deviations from PHASE.md

- None material. Additions beyond the letter of the doc: the
  `_LIVE_FEEDS` registry (so the phase-2 card can poll fresher state than
  the 1/s DB flush), the orphan sweep, and listing the two tools in
  luna-plugin.toml's curated tools section.

## Reassessment of remaining phases

- Phase 2 (progress card): unchanged, and slightly de-risked — the card
  route can read `_LIVE_FEEDS` first and fall back to the DB row.
  `current_phase()` helper already exists for the headline. Card route
  contract: unauthed `GET /api/p/plugin-playbooks/delegations/{id}/card?token=...`
  must constant-time-compare against `card_token`.
- Phase 3 (approval parking): unchanged. Note for its PHASE.md: while an
  approval card is pending the delegation still shows status "running" —
  the card should derive PARKED from the event feed (a prompt_always tool
  call event with no result yet for >Ns), not from a status value.
- Phase 4 (dojo e2e): unchanged.
