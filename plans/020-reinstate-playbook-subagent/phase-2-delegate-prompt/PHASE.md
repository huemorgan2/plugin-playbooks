# Phase 2 — the new delegate prompt + bench-readable tool stream

## Baseline (after phase 1, commit dd10ace)

pytest 331 green. Delegation mechanism live on QA Luna with the interim
prompt (old ~15-line rules + pasted 12KB authoring skill body).

## Scope

### A. `_delegate_prompt` rewritten wholesale (delegation.py)

The load-bearing artifact per the vision. Eleven sections (adopting the
plan_idea skeleton):

1. Role & conditions — background delegate, one job, budget named (40 tool
   calls / 15 minutes) with why.
2. The brief — task + optional target playbook + its manifest.
3. Done is an artifact contract — published version, or an explicit
   candidate stop, or an explicit failure report; never "probably fine".
4. Work loop as numbered phases with retry caps; step 1 mandates fetching
   `playbook_language_reference` just-in-time. The 12KB authoring skill
   body is NO LONGER pasted — `_delegate_prompt` loses its `skill_body`
   parameter; the prompt carries its own lean workflow contract and the
   pblang syntax comes from the reference tool.
5. Artifact-quality bar matching the plugin's lints.
6. Budgets & stop rules with rationale; defined LOSING exits (3 failed
   validates → re-derive from the reference; 3 failed spec runs →
   re-derive data paths; rank ~5 hypotheses before retrying; blocked →
   stop and report).
7. Consequential-action tiers: free / side-effecting / owner-decision
   (gated tools raise real approval cards — call and wait, never work
   around a refusal or a decline).
8. Worked examples: one full minimal pblang playbook, one bad→good
   contrast, one example final report.
9. Pre-publish checklist (6–8 lines) immediately before the publish
   instruction — includes "plan row first; publish takes plan_id;
   plan_finish after".
10. Final-report contract with a length cap; verbatim rule: "dry_run
    output is simulated — never report it as real".
11. Tail reminder, 3–5 lines verbatim.

Style rules enforced by tests where cheap: ≤5 emphasized (ALL-CAPS-word)
rule lines, ~5 hard negatives, numeric limits carry their rationale, the
authoring skill body is absent from the prompt.

### B. Tool-stream persistence for the bench (delegation.py)

- `_EventFeed` records per tool call: `args` (JSON, values capped ~200
  chars, keys matching token/secret/password/authorization/api_key
  redacted) on the call event, and `ok: bool` on completion (false when
  the result parses as JSON with an `error` key, or the content starts
  with "Error"). Additive keys on the existing event dicts — the card and
  existing consumers ignore them.

### C. Authed delegation API (routes.py)

- `GET /api/playbooks/delegations/{id}` (authed router): full record —
  task, playbook, status, steps_used, started/finished, result, events
  (full, with args/ok). dojoP grades through this.
- `GET /api/playbooks/delegations?limit=N` — newest-first summaries
  (id, task first 120 chars, playbook, status, steps_used, timestamps).

## Verification

- Prompt-shape tests: section presence/order markers, reference-tool
  mandate, no pasted skill body, tail verbatim, emphasized-line cap.
- Feed tests: args recorded + redacted + capped; ok true/false paths.
- Route tests: authed detail + list; 404 unknown id.
- Full suite green; live smoke on QA Luna: one real delegation creating a
  tiny playbook end-to-end (may stop at the publish approval card — that
  parked state is itself a pass for the waiting path).

## Non-goals

Card redesign / luna-service pass-through (phase 3); publish (phase 4).
