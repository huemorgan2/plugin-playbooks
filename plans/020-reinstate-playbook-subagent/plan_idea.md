# Plan 020 — Reinstate the playbook subagent, better than it was

Status: IDEA — ready for an executing agent.
Date: 2026-09-02. Roy's direction: reinstate the playbook subagent as the centerpiece of
the plugin — but move forward, not backward: undo what needs to be undone, and build on
the newer, better things (plan-gate publishing stays; the old thin delegate prompt does
not come back).

## Context

Commit `fc2e017` ("plans/016 phase 2", 2026-09-01) removed the playbook subagent as
"unused": `delegation.py` (654 lines — `playbook_agent`/`playbook_agent_status` via
ctx.agent.run_turn with explicit tools= allowlist, max_turns/token/timeout budgets),
`card.py` (279 lines — owner progress card with capability-token polling), their tool
registrations, the `playbook-delegation` SkillDef, models, routes, toml entries, and
tests (`test_delegation.py` 427 lines, `test_card_html.py`, `test_card_route.py`).

Since then every multi-step authoring job runs as a long chain of tool calls in the
OWNER'S conversation. The dojoP bench shows all four models degrading on playbook tasks.
The reversal is deliberate: a delegate gets a fresh context sized for exactly one
authoring job — room for a far better prompt than the main conversation can carry.

What we KEEP from the newer work (do not undo):
- Plan-gate publishing (a `playbook_plans` row required to publish; ONE approval card at
  publish showing plan+change; `plans_full_power` skips the card, never the plan row).
- Ops chat = exceptions only; the ops-mode machine stays dead (0.33.0). Do NOT
  resurrect `ops_provider.py` or anything ops-mode.
- The improved authoring skill body (decomposition lints, autonomy routing from phase 4).

What we UNDO: the subagent removal itself.
What we BUILD NEW: the delegate's prompt and its failure discipline.

## The job

### Step 1 — recover the raw material

`git show fc2e017^:plugin_playbooks/delegation.py` (likewise card.py, the three test
files, and the deleted hunks of agent_tools.py / __init__.py / models.py / routes.py /
luna-plugin.toml). `plans/013-playbook-subagent/PLAN.md` documents the original
mechanism. Treat all of it as raw material, not as a spec to match.

### Step 2 — reinstate the mechanism (this part restores nearly as-was)

- `delegation.py`, `card.py`, `playbook_agent` + `playbook_agent_status`, the card
  polling route, models, toml entries, and the three test files, green.
- The small `playbook-delegation` SkillDef (~1KB) gating `playbook_agent`. Its text
  should make delegation the advertised path for any non-trivial build/edit; the fat
  authoring skill remains for direct/trivial authoring.
- Keep: delegate never gets `send_chat_message`; allowlist = authoring tools +
  playbook_list/playbook_status + tools referenced by the target playbook's manifest;
  ~40-call budget with `{"_aborted": ...}` on breach.

### Step 3 — modernize the delegate for the plan-gate world

- ADD to the allowlist: `playbook_plan_write`, `playbook_plan_read`,
  `playbook_plan_finish`, `playbook_preflight`. The delegate writes the plan row itself
  — the plan IS authoring work. (Alternative — parent pre-writes the plan and passes
  its id — rejected: it splits one job's context across two agents.)
- `playbook_publish` stays in the allowlist; the owner's one approval card at publish is
  unchanged. Who does the typing changes; the gate does not.
- Loop the delegate drives: orient → author/edit → validate → dry_run → specs →
  preflight → plan → publish (or explicitly stop as candidate with a named reason).

### Step 4 — the NEW delegate prompt (the point of the whole plan)

The old `_delegate_prompt()` was ~15 thin lines plus the pasted 12KB authoring skill.
Replace it with a dedicated document. Full design rationale and research citations live
in the dojoP repo, `plans/0002-plugin-suites-playbooks/PLAN.md` Part C2; the skeleton:

1. **Role + operating conditions** (5–8 lines): one job; fresh context; *no
   conversation history, no owner in the loop, you cannot ask a question; your final
   text is the only thing that reaches the parent.* The ~40-call budget named here.
2. **The brief** (input slot): goal, target playbook (new vs edit), manifest intent,
   constraints — whatever the parent passes.
3. **Definition of done as an artifact contract**: validates clean → dry-runs over
   realistic inputs → specs green → preflight green → plan row written → published — or
   explicitly left candidate with a named reason. Artifact state, not "task complete".
4. **Work loop**: numbered phases, each with a one-line Goal, the tool that closes it,
   what passing looks like, and the failure branch with its retry cap. Phase 1 mandates
   `playbook_language_reference` — fetched just-in-time, NEVER pasted into the prompt
   (this kills the 12KB inline skill).
5. **Artifact-quality bar**: 5–7 positive-framed items (named meaningful step ids; no
   dangling `{{steps.*}}`/`{{inputs.*}}` refs; context economy; a send_chat_message
   step where the owner needs to see output; manifest alignment; minimal diff on
   edits). This list should match the plugin's own lints one-to-one.
6. **Budgets & stop rules, each with its rationale**: 3 failed validates → stop
   guessing, re-derive from the language reference; 3 failed spec runs → stop editing,
   re-derive the data paths; repeated failure → rank ~5 hypotheses, address the most
   likely first; genuinely blocked → stop and report. A defined LOSING exit — the old
   delegate's biggest hole (observed unbounded spec-debug loops).
7. **Consequential-action tiers**: free (validate/dry_run/spec_run) · real side effects
   (run_candidate) · owner-decision (publish card, edit_force, manifest_set,
   spec_delete — call them and wait; never work around a refusal).
8. **Worked examples**: one complete minimal pblang playbook; one contrastive bad→good
   snippet; one example final report. Examples before rules — pblang has no priors.
9. **Pre-publish gate**: a 6–8 line checklist physically immediately before the publish
   instructions.
10. **Final-report contract**: fixed skeleton — what shipped + version / evidence with
    the tool that produced it / decisions made unilaterally / what's left / risks.
    Length cap. Never paste playbook source. **dry_run output is simulated — never
    report it as real.**
11. **Tail reminder**: 3–5 lines, VERBATIM recaps only (publish gate, dry-run honesty,
    report shape) — no paraphrase.

Discipline: ≤5 emphasized lines in the whole prompt; ~5 hard negatives max, positive
framing elsewhere; every numeric limit carries its rationale; no motivational fluff; no
pasted reference docs; total rule mass well under the ~100-instruction adherence
ceiling.

### Step 5 — persistence for the bench

`playbook_agent_status`'s terminal record must retain the delegate's tool stream (tool
name + args + ok/err per call) and be readable via the plugin HTTP API — the dojoP bench
grades the delegate's work through it. If the restored delegation row already stores
this, just expose the read.

### Step 6 — verify & ship

- All plugin tests green (restored + existing; `test_ops_exceptions.py` stays green).
- Manual smoke on a real luna: non-trivial build request → playbook-delegation skill →
  playbook_agent → progress card updates → publish approval card → published; delegate
  report relayed by the parent.
- dojoP targeted `playbooks.*` run should recover (graders already accept
  `playbook_propose|playbook_agent`); full grader/suite modernization is dojoP-side
  (its plan 0002 Parts A+B), not this plan's job.
- Version bump (0.34.0) + marketplace publish per the usual flow.

## Out of scope

- Parent-side seed changes in luna core (when to delegate, honest relay) — dojoP plan
  0002 C3, separate.
- New bench tasks / grader families — dojoP-side.
- Any ops-provider / ops-mode restoration.

## Defaults chosen (flag if you disagree)

1. Delegate writes the plan row itself.
2. `playbook_propose` remains the trivial one-shot path; delegation is for multi-step
   builds/edits.
3. Plan-013 budgets (≈40 calls, token/timeout) kept initially; retune against the bench
   once the dojoP playbooks suite lands.
