# Playbooks — vision

Luna's founding complaint about the agents that came before it: **untamable
execution**. They follow prompts, not defined sequences. You cannot reliably say
"do A, then B in parallel with C, then D when both are done" — and mean it.

Playbooks are Luna's answer. **They are Luna's solid execution layer**: the place
where agent improvisation ends and engineered execution begins.

## The thesis

An agent is probabilistic. The work an owner depends on should not be. When
something matters enough that it must happen the same way every time — the weekly
report, the invoice chase, the deploy check — it graduates out of conversation
and into a playbook. From that moment it is no longer "ask the model and hope":
it is a compiled step graph that runs the same way tonight, next week, and after
the model under Luna changes.

Everything else in this file is a consequence of that thesis. Playbooks exist to
be:

- **As deterministic as possible.** A playbook is code (pblang, a restricted
  Python subset) compiled to a step graph — never free-running prompt-following.
  The graph, not the model's mood, decides what happens next. Where a step needs
  judgment, the judgment is fenced inside that step; the shape of the run is
  fixed.
- **Reliable.** A playbook that ran green yesterday runs green today. Specs
  (dry-run behavioral tests) and probes (are its tools installed and answering)
  hold the floor; the publish gate refuses anything that cannot prove itself.
- **Trusted in execution.** Trust is never a promise from the model — it is
  architecture, in Luna's spirit of *real approvals, not LLM promises*. Changes
  land as candidates, invisible to production. Only `publish` makes content
  live, through machine-checked gates: validation → specs → a green test run of
  that exact candidate → probes. The plan gate gives the owner one honest
  window per change — a plan row recording intent, one approval card showing
  plan and change together (`plans_full_power` may skip the card, never the
  row). The gates answer "is it safe?" mechanically; the owner is never asked
  to verify what a machine can verify.
- **Traceable.** "Why did you do that?" always has an answer. Every run, every
  version, every publish and rollback, every spec verdict is recorded and
  queryable — the database is the brain. History is symmetric: rollback goes
  through the same gate as publish, so the audit trail never has a gap.
- **Able to improve over time.** Versioned, rollback-able, never
  self-destructive. Failures of live runs surface as fix proposals — Luna finds
  its own problems and proposes its own fixes; the owner decides once per
  problem. Every improvement that survives becomes a spec, so quality only
  ratchets forward. A playbook a year old should be *better* than the day it
  was written, and provably so.

## The agent builds the playbooks

Part of the vision is that the owner does not write step graphs — **Luna does**.
Building its own tools is one of Luna's core convictions, and playbooks are the
purest form of it: the agent turns "what we do around here" into durable,
gated, testable execution.

The **authoring subagent** (`playbook_agent`) is how that stays excellent. It is
our way to buy clarity without overloading the main agent's context:

- Authoring a good playbook is a craft — reading intent, decomposing, writing
  pblang, validating, dry-running, spec-ing, passing the gates. Done in the
  owner's conversation it floods the context with dozens of tool calls and
  drowns the assistant the owner was actually talking to.
- The subagent runs the job in a **fresh, dedicated context** with a
  purpose-built prompt sized for exactly this craft — far larger and sharper
  than the main conversation could ever carry — plus an explicit tool
  allowlist and hard budgets. The main agent stays what it should be: the
  owner's counterpart, briefing the specialist and relaying its report.
- The owner keeps the same single window: a progress card while the delegate
  works, and the one publish approval at the end. Clarity for the owner,
  clarity for the model, one gate — that is the whole point.

Direct authoring tools remain for trivial one-shot jobs; the subagent is the
advertised path for everything else.

## Critical architecture — load-bearing, do not remove

These pieces are the vision's skeleton. They can look optional from usage
metrics alone; they are not. (This has happened: commit `fc2e017` removed the
authoring subagent as "unused" and every model's playbook competence measurably
dropped in the dojoP bench until it was reinstated — plans/020.)

1. **The authoring subagent** — the clarity mechanism above. If it looks
   unused, fix its adoption (skill text, parent seeds, bench); never delete it.
2. **The candidate → publish gate** — no path makes content live except
   `publish` through its gates. Convenience features route through it, never
   around it.
3. **The plan gate** — one plan row, one card, the owner's one honest window.
   "More cards" and "no record" are both failures.
4. **The manifest read-stage + drift check** — intent in plain text, read
   before every edit; silent drift from intent is the root of trust rot.
   `playbook_edit_force` is the deliberate, visible override — keep it loud.
5. **Specs as the regression floor** — the publish gate and `playbook_spec_run`
   stay the same code path, so "specs pass" always means what the gate means.

## Principles for changes

- **Serve the thesis.** Every change is judged by whether it makes execution
  more deterministic, more reliable, more trusted, more traceable, or better at
  improving — a feature that trades those away is not a feature.
- **Move forward, not backward.** Undo mistakes, keep improvements. A removal
  needs the same evidence bar as an addition: bench proof the product is better
  without it.
- **The bench is the referee.** Quality is measured by the dojoP playbooks
  suite (opt-in/opt-out, delegation, artifact quality), across models — green
  on one model while degrading others is not done.
- **Honesty over polish.** Dry-run output is simulated and reported as
  simulated. A candidate is never described as live. A failed gate is reported
  with its reason, never papered over.
- **Context economy.** Reference material is fetched just-in-time by tools,
  never pasted into always-loaded prompts.
