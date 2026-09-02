# Plan 020 — Reinstate the playbook subagent

Status: IDEA — ready for an executing agent.
Date: 2026-09-02. Requested by Roy: "bring playbooks back to what it was and reinstate
the playbook [subagent]". Restoration FIRST; the bigger prompt rewrite is a follow-up
(see dojoP plans/0002-plugin-suites-playbooks Part C in the dojoP repo).

## What happened

Commit `fc2e017` ("plans/016 phase 2", 2026-09-01) removed the playbook subagent as
"unused":

- `plugin_playbooks/delegation.py` — 654 lines: the `playbook_agent` /
  `playbook_agent_status` implementation (ctx.agent.run_turn delegate with explicit
  tools= allowlist, max_turns/token/timeout budgets, event-stream progress).
- `plugin_playbooks/card.py` — 279 lines: the owner-facing progress card
  (capability-token polling, unauthed route).
- The tool registrations in `agent_tools.py`, the `playbook-delegation` SkillDef wiring
  in `__init__.py`, delegation models in `models.py`, the card route in `routes.py`,
  `luna-plugin.toml` entries.
- Tests: `test_delegation.py` (427 lines), `test_card_html.py`, `test_card_route.py`.

Result: every multi-step authoring job now runs as a long chain of authoring tool calls
in the OWNER'S conversation. The dojoP bench matrix shows all four models degrading on
playbook tasks since. Direction is reversed: the subagent is the centerpiece of the
playbooks plugin — a fresh, dedicated context sized for exactly one authoring job.

## The job

Resurrect the subagent from `fc2e017^` and make it work on TOP of today's architecture
(0.33.0: plan-gate publishing, ops chat = exceptions only, no ops mode machine). This is
a restoration to functional parity, not a redesign. Do NOT resurrect `ops_provider.py`
or anything from the ops-mode machine — that removal stands (0.33.0 dropped its
successor too).

### Step 1 — recover the source

`git show fc2e017^:plugin_playbooks/delegation.py` (and card.py, and the three test
files, and the deleted hunks of agent_tools.py / __init__.py / models.py / routes.py /
luna-plugin.toml). Plan 013 (`plans/013-playbook-subagent/PLAN.md`) documents the
original mechanism and remains the reference.

### Step 2 — reinstate mechanically

- Restore `delegation.py`, `card.py`, the `playbook_agent` + `playbook_agent_status`
  tools, the card polling route, the models, the toml entries.
- Restore the small `playbook-delegation` SkillDef (~1KB) that gates `playbook_agent`
  in the main agent. The fat authoring skill stays for direct authoring, but delegation
  becomes the advertised path for any non-trivial build/edit.
- Restore the three test files and get them green.

### Step 3 — reconcile with the 0.33.0 plan-gate

The delegate's allowlist predates plan-gating. Update it:

- ADD `playbook_plan_write`, `playbook_plan_read`, `playbook_plan_finish`,
  `playbook_preflight` — the delegate writes the plan row itself (the plan IS authoring
  work) and drives the loop through preflight.
- `playbook_publish` stays in the allowlist; the one owner approval card at publish is
  unchanged — who does the typing changes, the gate does not. `plans_full_power`
  behaves exactly as in main-conversation flow (skips the card, never the plan row).
- Keep the original exclusions: the delegate never gets `send_chat_message`; allowlist
  = authoring tools + playbook_list/playbook_status + tools referenced by the target
  playbook's manifest.
- The delegate prompt in `_delegate_prompt()` mentions the old loop
  (read→edit→validate→dry_run→specs→publish); patch it minimally to name the plan-gate
  loop (…→specs→preflight→plan→publish). MINIMAL patch only — the full prompt rewrite
  is the follow-up plan, don't start it here.

### Step 4 — persistence for the bench (small but required)

`playbook_agent_status`'s terminal record must retain the delegate's tool stream
(tool name + args + ok/err per call) and be readable via the plugin HTTP API. The dojoP
bench will grade the delegate's work through it. If the restored code already keeps
this in the delegation row, just expose the read.

### Step 5 — verify

- Plugin tests green (restored + existing; `test_ops_exceptions.py` must stay green —
  we are not un-doing ops-exceptions).
- Manual smoke via a real luna: "build me a playbook that does X and Y" → main agent
  loads playbook-delegation → playbook_agent runs → progress card updates → publish
  card → published.
- dojoP bench: targeted `playbooks.*` run should recover (the graders accept
  `playbook_propose|playbook_agent`); full reconciliation of graders is dojoP-side work
  (dojoP plan 0002 Part B), not this plan's job.
- Version bump (0.34.0) + marketplace publish per the usual flow.

## Explicitly out of scope

- The new 11-section delegate prompt (dojoP plan 0002 Part C2) — follow-up.
- Parent-side seed changes in luna core — follow-up (dojoP plan 0002 C3).
- New bench tasks / grader families — dojoP-side (plan 0002 Parts A+B).
- Any ops-provider / ops-mode restoration.

## Open decisions (defaults chosen, flag if you disagree)

1. Delegate writes the plan row itself (chosen) vs parent pre-writes and passes plan id.
2. `playbook_propose` remains the trivial one-shot path (chosen); delegation is for
   multi-step builds/edits.
3. 40-call / plan-013 budgets kept as-is for parity; retuning belongs to the prompt
   rewrite.
