# Plan 020 — Reinstate the playbook authoring subagent (forward, not backward)

Status: approved by owner intent ("bring it back going forward"), authored by
this session after independent review. `plan_idea.md` in this folder was
treated as recommendations and data points, not as the plan; deviations from
its "Defaults chosen" are flagged in §Decisions.

## Why

- `vision/vision.md` lists the authoring subagent first under "Critical
  architecture — load-bearing, do not remove". Commit `fc2e017` deleted it by
  mistake (entangled with the ops_provider removal) and dojoP bench scores
  dropped across models.
- The owner's brief adds one requirement the old build never delivered: the
  chat progress card must actually work in the hosted product and look
  genuinely good. Plan 014 fixed the tenant base path (Part A, 0.25.3) but
  Part B — the luna-service cloud proxy letting the sandboxed card's
  token-gated GET through without a session — was never executed. Verified
  this session: `luna-service/cloud/api/proxy.py` still has only
  `_TOKEN_GATED_WS_SUFFIXES = ("api/p/luna-macrunner/ws",)`. Hosted cards
  therefore always fall into the honest-offline fallback. That is the "never
  really worked" the owner remembers.

## Raw material (recovered, in scratchpad `old-subagent/`)

`delegation.py.old` (654 lines), `card.py.old` (279), the three old test files,
plus the fc2e017 deletions in models.py / routes.py / __init__.py /
luna-plugin.toml. All read in full this session. `agent_tools.py` lost no
delegation code (delegation lived in its own module) — its fc2e017 hunks were
ops-only and stay dead.

## Verified seams (current luna core + plugin, 2026-09-02)

- `ctx.agent.run_turn` still accepts `tools=`, `max_turns`, `timeout_s`,
  `event_stream_handler`, `conversation_state`, `conversation_id`,
  `memory_read/write` (`luna/plugins/agent_facade.py:43`). `ctx.post_chat_card`
  still exists (luna_sdk re-export).
- `ToolDef.modes` exists but the vocabulary changed: since luna 098 the only
  states are `planning` / `building` (`_ALL_MODES` in agent_tools.py:1705).
  `fix_approve` / `fix_publish` are gone — the old delegation ToolDef's
  `modes=["building","fix_approve","fix_publish"]` must NOT come back.
- Plan-gate tools exist and are NOT in `AUTHORING_TOOLS`:
  `playbook_plan_write` / `playbook_plan_read` / `playbook_plan_finish`
  (agent_tools.py:1749/1812/1877, `modes=_ALL_MODES`, not skill-gated).
  `playbook_preflight` IS in `AUTHORING_TOOLS` already.
- `playbook_publish` requires `plan_id` (plan row machine-checked); one
  approval card at publish; `plans_full_power` skips only the card, never the
  row.
- Baseline: pytest **302 passed** on 0.33.0 (`b430e72`), vitest 126, tsc clean.

## Decisions (and flags against plan_idea's "Defaults chosen")

1. **Agree**: the delegate writes the plan row itself (`playbook_plan_write`
   before publish, `plan_finish` after). The main conversation only phrases
   the task.
2. **Agree**: `playbook_propose` stays the trivial one-shot path; delegation
   is the default for real authoring jobs (skill descriptions steer this,
   restored from the pre-fc2e017 text).
3. **Agree**: keep plan-013 budgets initially — `_MAX_TURNS=40`,
   `_TIMEOUT_S=900`, breach → `{"_aborted": ...}` honesty path.
4. **Deviation (modernization, flagged)**: ToolDef modes become
   `["planning","building"]` (the entire current vocabulary) for both
   delegation tools — the old fix_* modes no longer exist. The headless
   delegate turn passes `conversation_state="building"` so publish-class
   tools are available inside it.
5. **Addition (owner requirement, beyond plan_idea)**: the chat card gets its
   own phase — luna-service Part B (token-gated HTTP pass-through preserving
   `ACAO:*`) plus a visual refresh of `card.py`. plan_idea only carried the
   bench-persistence part of the card story.
6. **Agree**: tool-stream persistence (name + args + ok/err per call) in the
   terminal delegation record, readable over HTTP for the dojoP bench. Old
   events had label/detail/ms but not args — schema extends, additive.

## Hard constraints (from vision + owner brief — apply to every phase)

- No path to live except `playbook_publish` through the machine-checked gates.
- The plan row is never skipped; `plans_full_power` skips only the card.
- `dry_run` output is simulated and must be reported as simulated by the
  delegate's final report.
- Delegate never gets `send_chat_message`.
- Do not touch luna core seeds, the dojoP bench repo, or anything ops-mode;
  `ops_provider.py` stays dead; `test_ops_exceptions.py` stays green.
- Ship as **0.34.0** (three stamps), push `origin main`, publish to `official`.

## Phases

### Phase 1 — Reinstate the mechanism, modernized for the plan-gate world

Bring back, adapted (not reverted):

- `plugin_playbooks/delegation.py` from `delegation.py.old`, with:
  - ToolDef modes → `["planning","building"]` on `playbook_agent`;
    `playbook_agent_status` likewise (it is read-only, available everywhere).
  - `delegate_toolset(...)` allowlist = AUTHORING_TOOLS (incl. preflight)
    + `playbook_list`/`playbook_status` + referenced tools, **plus**
    `playbook_plan_write`, `playbook_plan_read`, `playbook_plan_finish`;
    still filters `send_chat_message`.
  - `_drive_delegation` passes `conversation_state="building"`.
  - Keep: capability token, `_LIVE_FEEDS`, phase inference, waiting-on-owner,
    orphan sweep, the running/waiting status messages, `.result`/`.part`
    duck-typing.
  - The old `_delegate_prompt` comes back only as a placeholder seam — the
    real prompt is phase 2's deliverable.
- `PlaybookDelegation` model in models.py (same columns) + column-migration
  hook if needed; `luna-plugin.toml`: `db_tables` += `playbook_delegations`,
  the two `[[tools]]` entries, tools/tables counts.
- Unauthed card route `GET /delegations/{delegation_id}/card` in routes.py
  (compare_digest, single 404 shape, ACAO:*, no-store) — serving the OLD card
  for now; phase 3 replaces the HTML.
- `__init__.py`: `_DELEGATION_SKILL_BODY` + `playbook-delegation` SkillDef +
  `DELEGATION_TOOLS` + `_register_tool` gating + on_load registration + sweep
  + restore the "load playbook-delegation instead" steering in the
  playbook-authoring skill description. Skill body updated where the world
  changed (plan-gate mention).
- Tests: adapt the three old test files (delegation, card html, card route) to
  the current tree; whole suite green including `test_ops_exceptions.py`.

Verify: full pytest; plugin loads on QA Luna (:8765) with 28 tools and the
`playbook_delegations` table present (direct SQL check).

### Phase 2 — The new delegate prompt + bench-readable tool stream

The point of the reinstatement per vision: the delegate's system prompt is a
first-class artifact, written new (the old one was ~15 lines + a pasted 12KB
skill). Eleven sections per the plan_idea skeleton (adopted — it is good):

1. Role & conditions (budget named: 40 calls / 900s and why).
2. The brief (task slot + playbook slot).
3. Done is an artifact contract (published version / candidate stop /
   explicit failure report — never "probably fine").
4. Work loop as numbered phases with retry caps; phase 1 mandates
   `playbook_language_reference` fetched just-in-time — the pblang reference
   is NEVER pasted into the prompt.
5. Artifact quality bar (matches the plugin's lints).
6. Budgets & stop rules with rationale; defined LOSING exits: 3 failed
   validates → re-derive from the reference; 3 failed spec runs → re-derive
   data paths; rank ~5 hypotheses before the next attempt; blocked → stop and
   report.
7. Consequential-action tiers (free / side-effects / owner-decision — gated
   tools raise real approval cards; wait, don't work around).
8. Worked examples: one full minimal pblang playbook, one bad→good pair, one
   example final report.
9. Pre-publish checklist (6–8 lines) immediately before the publish
   instructions — including "write the plan row first; publish requires
   plan_id".
10. Final-report contract with a length cap and the line "dry_run output is
    simulated — never report it as real".
11. Verbatim 3–5 line tail reminder.

Style rules: ≤5 emphasized lines, ~5 hard negatives, every numeric limit
carries its rationale.

Persistence for the bench: extend `_EventFeed` to record tool args (truncated,
secrets-free — reuse the plugin's existing arg-scrub if present, else cap +
drop values for keys matching token/secret/password) and ok/err per call;
terminal record exposes the full stream. Add an **authed**
`GET /api/p/plugin-playbooks/delegations/{id}` JSON route (list route optional)
so dojoP grades through HTTP without DB access.

Verify: prompt-shape unit tests (sections present, reference not inlined, tail
verbatim); feed tests for args+ok/err; route test; full suite green. Live
smoke on QA Luna: one real `playbook_agent` delegation end-to-end (create a
tiny playbook, plan row written, publish approval card raised).

### Phase 3 — The chat card: make it work, make it beautiful

Two halves:

**A. luna-service Part B (the fix that never landed).** In
`cloud/api/proxy.py`, add a token-gated HTTP pass-through mirroring the
`_TOKEN_GATED_WS_SUFFIXES` precedent: GET requests whose path matches
`api/p/plugin-playbooks/delegations/` + `/card` (and only that suffix
family) with a `token` query param skip the session requirement and proxy
straight through, preserving the plugin's response headers (`ACAO:*`,
`no-store`) so the sandboxed iframe's fetch succeeds. Auth still lives in the
plugin (compare_digest, single 404). Tests in luna-service; commit + push.
Deployment of luna-service follows its usual pipeline — noted for the owner
in the summary if a manual deploy step is theirs.

**B. Visual refresh of `card.py`.** Keep everything honest from 0.25.3
(API_BASE resolution, readable-401/403 → offline, never-succeeded → offline
copy, height postMessage) and the eyebrow/headline/support/phases/feed/result
grammar, but redesign the rendering: modern dark card, clear phase timeline
with state transitions, live "what it's doing now" line, animated-but-calm
progress affordance, tidy collapsible feed with per-call ok/err ticks (from
phase 2's richer events), distinct terminal states (done / failed / needs
owner) with the result up front. Self-contained srcdoc, no external fetches
except the poll.

Verify: card HTML unit tests updated; rendered card exercised against QA Luna
via headless browser (CDP) hitting the real unauthed route — poll succeeds,
phases advance, terminal state renders; a copy of the srcdoc opened with a
wrong token shows the offline state (single-404 oracle preserved). Full suite
green.

### Phase 4 — End-to-end, ship 0.34.0, publish

- Fresh QA Luna run of the complete story: main-agent loads
  playbook-delegation skill → `playbook_agent` → card posts → delegate
  authors, validates, dry-runs (reported as simulated), specs, preflight,
  plan row, publish approval card → approve → live; `playbook_agent_status`
  and the authed delegations route return the full tool stream.
- Regression sweep: full pytest, vitest, tsc, vite build.
- Bump the three stamps to 0.34.0, commit, push `origin main`, package +
  publish to `official`, verify catalog `latest_version == 0.34.0`, swap the
  managed copy on QA Luna, remind the owner to upgrade their agents.

## Out of scope

Luna core seeds, dojoP bench repo itself, ops-mode anything, old cards baked
into past chat history (unfixable by design — plan 014), non-playbooks
luna-service routes.

## Versioning note

Phases 1–3 commit and push but do not publish; 0.34.0 publishes once at
phase 4 after the end-to-end pass — a half-reinstated subagent must never hit
the marketplace.
