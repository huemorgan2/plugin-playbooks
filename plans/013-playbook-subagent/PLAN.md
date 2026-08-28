# 013 — playbook sub-agent: focused delegate with a live progress card in chat

Status: PLANNED 2026-08-28. Depends on plans/012 (no point delegating the
same slow loop — fix the round trips first, then isolate).

## Why a sub-agent (and why in-Luna, not external)

Measured (see plans/012 evidence): playbook authoring drags a ~24KB skill,
~20KB read stages and ~11KB definitions through the MAIN conversation's
context, on top of everything else the owner's agent carries. The model is
not struggling with the domain — its edits and diagnoses in the observed
chat were correct — so an external coding agent (pi.dev-style) buys
nothing and loses the validate → dry-run → specs → promote loop that
actually guards quality. What isolation buys:

- **Context hygiene:** the main conversation keeps ONE tool call + ONE
  result; skill, cheatsheet, read stages, spec YAML all live and die in
  the delegate's context.
- **Focus:** the delegate sees only playbook tools (+ read-only tools of
  the integrations the playbook touches) — no tool-choice noise, no
  tasks ceremony (its progress card replaces the checklist).
- **Freedom to iterate:** 10 edit→validate→spec cycles cost the main
  conversation nothing.

## Shape

### New tool: `playbook_agent`

Skill-gated alongside the other authoring tools. Signature:
`playbook_agent(task, playbook="", wait_seconds=25)`.

- Creates a `PlaybookDelegation` row (id, task, playbook, status
  running|done|failed|needs_owner, started_at, finished_at, result,
  events JSON).
- Spawns a nested agent loop via luna's `PluginAgentFacade.run_turn`
  (same seam `agent_step` in the runner uses today), with:
  - system context = the `playbook-authoring` skill + the task + the
    target playbook's manifest;
  - toolset = playbook tools only, plus the read-only tools the target
    playbook's steps reference (derived from its `tool_call` step tools);
  - a hard step budget (default 40 tool calls) — over budget → status
    `needs_owner` with a summary of where it stopped.
- Follows `playbook_run`'s async pattern: wait up to `wait_seconds`,
  else return `{delegation_id, status: running}`; `playbook_agent_status`
  polls. The main agent is instructed by the tool result to tell the
  owner the card below tracks progress, then END ITS TURN — not poll.

### Progress card in chat (the owner-facing UI)

One message with `embed_iframe` → `/api/p/plugin-playbooks/ui/delegation/
{id}` posted when the delegation starts; the widget polls
`/api/p/plugin-playbooks/delegations/{id}` (1–2s while running). No
second message per phase — the CARD is the live surface.

Per vision/ux_guidelines.md (read before executing — mandatory), the card
uses the standard grammar, dark tokens, one gradient max (none here —
it's not a hero):

- **Eyebrow:** `PLAYBOOK AGENT` + the playbook name as a chip.
- **Headline (bottom line first):** the current state as a fact, not a
  log line — "Fixing update_phone — specs 6/8 passing", then
  "Done — candidate v20 promoted" / "Stopped — needs your call on X".
- **Support line:** one dim sentence — what it is doing right now.
- **Phase list:** one line each, status dots (hollow/amber/green/red):
  `Understand` (read definition + failed run), `Change` (edits),
  `Prove` (specs/dry-runs, shown as "n/m passing"), `Ship` (promote).
  Phases come from a fixed vocabulary the delegate's events map onto —
  never free-text internal codes (jargon rule; the enum values ARE
  owner words, per the vocabulary-fix principle).
- **Expandable detail per phase:** the event feed — each event one line:
  tool called, verdict, duration; reasoning summaries (the delegate's
  inter-tool text, first line only) in dim type. Collapsed by default.
- Timings: total elapsed in the header; per-event durations in detail.

### Events

The delegate loop records an event per tool call and per assistant text:
`{ts, phase, kind: tool|thought, label, detail, ms}`. Phase is inferred
server-side from the tool name (read/get_definition → Understand,
edit/manifest → Change, spec_*/dry_run/validate → Prove,
promote/rollback → Ship) — the model never emits phase codes itself.

### Failure + approval paths

- Tools with `policy="prompt_always"` (spec_delete, promote today) keep
  their approval cards — the delegation parks in `needs_owner`, the
  progress card headline says so, and resumes when the approval lands
  (approval gates park turns; the card must reflect PARKED, not stuck).
- Delegate crash/step-budget → status `failed`/`needs_owner`; the main
  agent's next turn surfaces the summary; the card shows the bottom line.

## Phases

1. **Delegation core** — table + `playbook_agent`/`playbook_agent_status`
   tools, nested loop, toolset scoping, step budget, events recorded.
   Acceptance: unit tests with a scripted fake agent; contract test that
   the main-turn payload stays under ~1KB.
2. **Progress card** — routes (status JSON + iframe HTML), the widget
   (ui-src), tokens + grammar per ux_guidelines. Cache-bust with
   `?v=<manifest.version>`. Acceptance: widget renders all states from
   fixture JSON; screenshot review against ux_guidelines checklist.
3. **Approval parking** — needs_owner round-trip with plugin_approvals;
   dojo test: delegate hits promote → owner approves from the card
   context → delegation completes.
4. **Dojo end-to-end** — a real browser conversation on QA Luna: "fix the
   phone format in candidate-intake" delegates, card streams, result
   lands; compare wall-clock + main-context tokens vs the plans/012
   baseline chat.

## Non-goals

- Running the delegate on an external service (rejected — see 007.012
  build-vs-reuse discussion and the evidence above).
- Replacing dojo/spec machinery. The delegate USES the same tools.
- Multi-delegation concurrency UI (one card per delegation is enough;
  concurrent delegations just mean two cards).

## Open questions for execution time

- `PluginAgentFacade.run_turn` reentrancy under `luna serve` (bootstrap
  loop memory: on_load tasks die silently — the delegate must run inside
  a request-scoped task or the scheduler, not on_load).
- Whether muted-turn rules apply to the delegate (it must never be
  gated behind a muted-turn-invisible tool — degrade-visible rule).
