# Playbooks — vision

Playbooks is Luna's flagship plugin: durable, multi-step agent workflows the owner can
trust to run unattended. Everything below is the durable shape of the product. Plans
come and go; this file is what a change must be measured against.

## What a playbook is

- **Code, never executed as code.** pblang (a restricted Python subset) compiles to a
  step graph. The artifact is reviewable, diffable, and rollbackable.
- **Intent in plain text.** The manifest states what the playbook is for and its hard
  constraints; the edit flow forces reading it first, and drift against it blocks edits.
- **Guarded by evidence.** Specs (dry-run behavioral tests) and probes (are the tools
  installed and answering) gate every publish. Changes land as candidates; only
  `publish` makes them live, and only through the gates.
- **Owner-approved.** Publishing runs through the plan gate: a plan row records the
  intent of the change, and ONE approval card shows the owner plan + change together.
  `plans_full_power` may skip the card — never the plan row.

## Critical architecture — load-bearing, do not remove

These pieces look optional from usage metrics alone. They are not. Removing any of them
degrades the whole product (this has happened: commit fc2e017 removed the authoring
subagent as "unused" and every model's playbook competence measurably dropped in the
dojoP bench until it was reinstated — plans/020).

1. **The authoring subagent (`playbook_agent` / delegation.py).** THE centerpiece of
   authoring. Multi-step builds and edits run in a delegate with a fresh context, a
   dedicated purpose-built prompt, an explicit tool allowlist, and hard budgets —
   instead of flooding the owner's conversation with dozens of authoring calls. The
   fresh context is the point: it carries a far better, larger, job-specific prompt
   than the main conversation ever could. Direct authoring tools remain for trivial
   one-shot jobs; the subagent is the advertised path for everything else. Any future
   "lean" pass that finds it unused should fix its adoption (skill text, parent seeds,
   bench), not delete it.
2. **The candidate → publish gate.** No path may make content live except `publish`
   through validation → specs → test-run → probes. Convenience features route through
   it, never around it.
3. **The plan gate.** The plan row + single approval card is the owner's one honest
   window into a change. Weakening it to "more cards" or "no record" are both wrong.
4. **The manifest read-stage + drift check.** Edits that skip reading intent, or that
   silently contradict it, are the root of trust rot. `playbook_edit_force` is the
   deliberate, visible override — keep it loud.
5. **Specs as the regression floor.** Publish-gate evaluation and `playbook_spec_run`
   must stay the same code path, so "specs pass" always means what the gate means.

## Principles for changes

- **Move forward, not backward.** Undo mistakes, keep improvements. A removal needs the
  same evidence bar as an addition: bench proof that the product is better without it.
- **The bench is the referee.** plugin-playbooks quality is measured by the dojoP
  playbooks suite (opt-in/opt-out, delegation, artifact quality). A change that greens
  the suite on one model but degrades others is not done.
- **Honesty over polish.** Dry-run output is simulated and must always be reported as
  simulated. A candidate is never described as live. A failed gate is reported with its
  reason, never papered over.
- **Context economy.** Reference material (pblang docs, language reference) is fetched
  just-in-time by tools, never pasted into always-loaded prompts.
