# Phase 4 — dojo end-to-end: execution summary

## What shipped

Still version **0.25.0** (the plan-close publish covers phases 2–4; nothing
shipped alone). One real code change came out of this phase — found in the
dojo, not planned:

- `plugin_playbooks/__init__.py` — both skill descriptions rewritten to fix
  a steering bug (see "The find" below). `playbook-delegation` now presents
  itself as **the DEFAULT** for any create/fix/change job; `playbook-authoring`
  presents itself as the **INLINE** path, to load only when the owner asked to
  work through the playbook together step by step, and it points at
  playbook-delegation for everything else.
- `tests/test_delegation.py` — regression test
  `test_skill_descriptions_steer_playbook_jobs_to_delegation` pinning the
  steering contract (authoring says "inline" and names playbook-delegation;
  delegation says "default"; the "any playbook" phrasing that caused the miss
  is banned from authoring's description).

Full suite after the change: **252 passed**.

## The fixture

`candidate-intake` playbook on QA Luna: one code step (`update_phone`) that
normalizes phone numbers to E.164, two specs (US + UK number), promoted
green. Broken for each run by renaming the step's `code_inputs` wiring to
`{{ inputs.phone_number }}` (the schema only defines `inputs.phone`), via the
owner-edit route `PUT /api/p/plugin-playbooks/playbooks/candidate-intake` —
after the break both specs fail (0/2) with a diagnosable UndefinedError
naming the available variables.

**Deviation from PHASE.md**: the plan said "edit the code so the phone format
comes out wrong". That break is invisible to specs: specs are *structural*
dry-runs — `_run_code` renders `code_inputs` templates but never executes the
code body in dry-run (the step under test is stubbed). Broken **input
wiring** is the class of bug specs can actually catch, so that is what the
fixture breaks. This is a property of the spec design worth remembering, not
a spec bug.

## Baseline run (fix in the main context)

Fresh conversation, prompt: *"fix the phone format in candidate-intake and
make it live — do the work yourself in this chat, don't hand it to the
background agent."* Luna loaded playbook-authoring and ran the loop inline:
get definition → spec list → edit → spec run → validate → promote (v2→v3).

- Wall-clock: **30 s**
- Tool calls through the owner conversation: **7**
- Done event `context_input_tokens`: **28,544**
- Tool-result traffic through the owner context: ~4,686 tokens

## The find: natural phrasing did not delegate (fixed)

First delegated attempt (same fixture re-broken, v4) used the natural
phrasing *"The candidate-intake playbook stopped working — the phone step is
broken. Fix it and make the fix live."* Luna loaded **playbook-authoring**
and fixed it inline (v4→v5): 6 tool calls, `context_input_tokens` 28,343 —
no delegation, no card (`p4-browser-01-parked.png` shows only the approval
card). Root cause: authoring's old description claimed "load before creating
or modifying **any playbook**", beating delegation's circular "…and you are
not authoring it inline" at delegation's own headline scenario.

Fix at the vocabulary level (per the flows-belong-in-tool-layer lesson):
the two descriptions now partition the space — delegation is the default,
authoring is the explicit build-it-together path. Synced to managed_plugins,
server restarted, retested.

## Delegated run (take 2, after the fix)

Fresh conversation, same natural phrasing, fixture re-broken (v6).

- Luna's main turn made exactly **2** tool calls: `load_skill`,
  `playbook_agent` — then ended its turn with the card posted.
- Main-turn wall-clock: **36 s**; done event `context_input_tokens`:
  **19,512** (vs 28,544 baseline — ~32% smaller, and flat: the edit/spec
  loop never touches the owner context).
- Delegate (id `4dae5e2b-6782-4a71-a3a9-c09340f41fee`): 10 steps,
  Understand 4 → Change 2 → Prove 3 → Ship 1, finished **done** in 3 m 38 s
  — that figure includes the parked wait for the owner's approval click, so
  it is not comparable to the baseline's 30 s.
- In the real browser (CDP, zero console errors across every screenshot):
  - streaming: card live-updating mid-run (`p4-browser-02-streaming.png`)
  - parked: amber banner "Waiting for your approval — make the change live",
    Ship dot amber, approvals card below (`p4-browser-03-parked.png`,
    `p4-browser-02-card-phases.png`)
  - approve: clicked "Just this once" on the chat approval card → delegate
    resumed
  - done: "Done — candidate-intake updated", all four phases green with real
    step counts, "What it did" report, approval chip flipped to green
    "Approved · playbook_promote" (`p4-browser-06-done-card-top.png`,
    `p4-browser-05-done-card.png`)
- End state verified via API: candidate-intake **v7 live**, wiring
  `{{ inputs.phone }}`, specs **2/2 green**.

## Comparison (the claim under test)

| | baseline (inline) | delegated |
|---|---|---|
| owner-context tool calls | 7 | 2 |
| `context_input_tokens` at done | 28,544 | 19,512 |
| owner-turn wall-clock | 30 s | 36 s |
| where the fix loop ran | owner context | background delegate |

The delegated main turn stays small and *constant* — a bigger fix grows the
delegate's context, not the owner's, and the card carries the detail. The
accidental take-1 run (28,343 tokens, 6 calls) is a second baseline data
point confirming the inline cost is stable.

## Surprises / learnings

- **Approving stale pendings resumes parked turns.** A blanket auto-approve
  pass hit leftover approvals from the fixture-*setup* conversation, which
  resumed that parked turn mid-phase; it then tightened the specs and
  flipped `agent_autonomy` concurrently. Handled by letting it settle and
  re-verifying the fixture. Lesson: scope approval auto-approvers to the
  conversation under test, or clear stale pendings first.
- The SPA conversation route is `/chat/<id>` — `/c/<id>` silently lands on
  the greeting screen.
- Skill descriptions are load-bearing routing logic: the dojo caught in one
  turn what 252 unit tests could not, because the miss lives in how the
  model reads two descriptions side by side.

## Reassessment of remaining phases

Phase 4 was the last numbered phase. Remaining is the plan close, unchanged:
merge `013-playbook-subagent` → main, push (huemorgan2), publish **0.25.0**
to marketplaces.com.ai, master execution summary, PLAN.md → EXECUTED,
remove the worktree.
