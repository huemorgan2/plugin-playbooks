# 002 — code-authored playbooks + the validation engine

**Status: DRAFT — awaiting owner review.**

Owner ask: make playbooks trustworthy. The agent authors in Python (not
YAML-item surgery), every playbook carries a free-text manifest the agent must
read and the user can edit, changes land on a candidate version that is
tested (specs + connection probes) before being promoted to live, rollback is
one click, and the UI shows earned validation state everywhere. Replace the
run "playback/replay" experience with a plain step-execution view.

Research: `research/playbook-validation/idea.md` (ideas 1–7). This plan turns
it into phases. **Python-first**: the authoring layer is phase 1 because every
later gate (specs, drift, promote) operates on what the agent writes, and we
want that to be code from day one.

---

## The target lifecycle (how all parts play together)

One picture to keep in mind; every phase builds a piece of it:

```
agent: playbook_edit(name)                      ── READ STAGE
  ← edit ticket + manifest (free text) + current CODE (generated from IR)
    + latest spec results + preflight summary

agent: playbook_edit(name, ticket, code=...)    ── WRITE STAGE
  → compile code (AST, sandboxed, no tools)  → PlaybookDef IR
  → static validate (existing validator, on IR)
  → manifest drift check (LLM, free-text manifest vs diff)
  → specs auto-run (stubbed dry-run + assertions)
  all green → saved as CANDIDATE version (never live)
  any red   → refusal + steering hint (fix code | update manifest w/ approval)

agent/user: playbook_promote(name)              ── PROMOTE GATE
  → re-check: static ✓ specs ✓ preflight probes ✓ drift resolved ✓
  → candidate becomes live (playbook_versions row, promoted_from lineage)
  → triggers/runs now use it

anything wrong later: playbook_rollback(name)   ── one click / one tool call
  → previous live version restored (data already in playbook_versions)
```

The **IR (`PlaybookDef` YAML) stays canonical** — stored, versioned, rendered
on the canvas, validated. Python is the authoring surface; the canvas is the
viewing surface; they meet at the IR.

---

## Phase 1 — Python authoring layer (compile to IR, round-trip codegen)

The foundation everything else assumes.

### 1a. The language (restricted definition layer)

A module-level Python subset; combinators map 1:1 to existing step kinds so
the canvas keeps working unchanged:

```python
# inputs: week (str)
leads  = tool("monday_list_items", board=12345, week=inputs.week)   # tool_call
digest = llm("Summarize these leads for #sales", data=leads.items,   # llm_step
             output={"text": "string"})
if_(leads.count == 0,
    then=[tool("slack_post_message", channel="#sales", text="no leads")],
    else_=[tool("slack_post_message", channel="#sales", text=digest.text)])
```

- `tool()`, `llm()`, `agent()`, `if_()`, `loop()`, `parallel()`, `approve()`,
  `wait_event()`, `subtask()`, `state()`, `halt()` — exactly the KIND_KEYS
  table, nothing more.
- Assignments create the reference namespace: `leads.items` compiles to
  `{{steps.<id>.items}}`. Step ids derive from variable names (stable across
  recompiles — required for run history and canvas diffing).
- Raw `if`/`for`/`while` **between steps is rejected** at compile with a
  message naming the combinator to use. Arbitrary expressions allowed only
  inside argument positions (compiled to Jinja expressions).
- Use-before-define, unknown tool names, bad refs become **compile errors
  with line numbers** — the feedback loop the YAML DSL never had.

### 1b. Compiler + codegen

- `compiler.py`: Python source → AST parse (never executed — `ast` module
  only, whitelist of node types) → `PlaybookDef` IR. Deterministic.
- `codegen.py`: IR → Python source (for playbooks authored before this, or
  edited via canvas). Round-trip property test: `codegen(compile(src))`
  stable, `compile(codegen(ir)) == ir` for every existing playbook fixture.
- Legacy: existing playbooks get code lazily via codegen on first edit;
  nothing migrates in place.

### 1c. Tool surface changes

- `playbook_propose` / `playbook_edit` accept `code=` (Python). YAML input
  stays accepted during transition, flagged deprecated in the tool
  description; removed in phase 6.
- `playbook_get_definition` returns code (plus IR YAML on request).
- Snippet-diff editing (`old=`/`new=` text replacement against the code)
  ships here too — cheap once code is the medium.

**Deliverables:** `compiler.py`, `codegen.py`, round-trip tests over all
existing playbook fixtures, updated tools. No DB change. No UI change yet
(canvas still renders IR).

---

## Phase 2 — Manifest (free-text) + staged edit flow

### 2a. Manifest

- **Plain free text (markdown)** — the user's call, and the right one: the
  drift check is an LLM judgment anyway, and free text is what users will
  actually write and edit. Suggested (not enforced) sections: *Purpose*,
  *Side effects*, *Never* (invariants), *Acceptance*. No schema, no parsing.
- Storage: `manifest TEXT` column on `playbooks`; snapshot column on
  `playbook_versions` (travels with definition history).
- User-editable in the UI (2c) and via `playbook_manifest_set` (agent tool,
  `prompt_always` policy — manifest changes are owner-approval territory).

### 2b. Staged edit flow (gates, not prose)

- `playbook_edit(name)` with no payload → returns manifest + code + latest
  spec/probe summary + a short-lived **edit ticket** (row in a new
  `playbook_edit_tickets` table or in-memory with TTL).
- `playbook_edit(name, ticket, code|old/new)` → the write stage; refuses
  without a valid ticket. Compile → validate → **drift check**: one cheap LLM
  call, "does this diff conflict with the manifest? answer conflict|ok +
  one-line reason". Conflict → refusal with the reason and two legal moves
  (fix code, or `playbook_manifest_set` which triggers an approval card).
- `force=true` exists on save, but raises an approval card — skipping is a
  user decision, recorded in the version `message`.

**Deliverables:** manifest columns + migration-on-enable, ticket mechanism,
drift check, updated tool descriptions (stage-aware). Depends on phase 1
(the ticket returns *code*).

---

## Phase 3 — Candidate versions, promote, rollback

Mostly wiring what exists (`playbook_versions`, promote route, drafts).

- New columns on `playbooks`: `live_version INT`, `candidate_version INT|null`.
  A save (phase 2 flow) writes a `playbook_versions` row and sets
  `candidate_version`; **live is untouched**.
- Runner/trigger path always executes `live_version`. `playbook_dry_run`,
  spec runs, and preflight accept `version=candidate`.
- New tools: `playbook_promote(name)` (gate: static ✓ specs ✓ probes ✓ drift
  resolved; refuses otherwise with the failing gate named),
  `playbook_rollback(name)` (live ← previous version, `promoted_from`
  lineage as today). `playbook_run(name, candidate=true)` for a manual
  supervised test run of the candidate (approval-gated, tagged in
  `playbook_runs`).
- One candidate max per playbook. A new save overwrites the candidate
  (previous candidate still in `playbook_versions` history).
- REST equivalents for the UI; existing `POST /{name}/promote` route absorbs
  the gate.

**Deliverables:** columns, tools, runner pinning to live_version, run tagging.

---

## Phase 4 — Specs engine (fixture simulation with assertions)

- `dry_run` grows `stubs=` (step-id or tool-name → scripted output; falls
  back to shape-derived placeholder) and returns the trace as today.
- Spec format (YAML, stored in new `playbook_specs` table:
  `playbook_id, name, spec JSONB, created_by, updated_at`):
  `inputs`, `stubs`, `expect` (status, branch taken, per-tool call count +
  `args_contain`, `output_contains`). Assertion evaluator is a pure function
  over trace + references.
- Tools: `playbook_spec_add/list/run`. `playbook_spec_run` runs all specs
  against **the candidate** by default.
- **Auto-run on save** (plugs into phase 2's write stage) and **gate on
  promote** (phase 3).
- **Record & replay**: `playbook_spec_from_run(run_id)` — converts a real
  run's recorded `playbook_step_runs.outputs` into a spec's stubs, with
  `expect` seeded from what the run actually did (user/agent trims it).
  This is the cheapest path to real-shaped fixtures.

**Deliverables:** stubs seam, spec table + tools, assertion evaluator,
save/promote integration, run-to-spec converter.

---

## Phase 5 — Preflight probes (connections + access)

- SDK addition: optional `probe` recipe on a tool (declared by the owning
  plugin): `kind: auth|resource_read|permission|api_dry_run`, plus a callable
  or arg-template. v1 ships **auth + resource_read only** — no mutating
  probes.
- `playbook_preflight(name, version=candidate|live)`: walk the IR, collect
  tool_calls + agent-step tool allowlists, resolve static args from the
  dry-run trace, execute each tool's probe, return per-tool
  ✅/⚠️(unprobeable)/❌ with the failure class (credential dead / resource
  gone / permission downgraded / rate limited).
- Results cached in `playbook_probe_results` (playbook_id, tool, status,
  detail, probed_at) — feeds the UI badges and the promote gate.
- **Scheduled re-probe**: daily cron (existing trigger source) over active
  playbooks; failures notify chat *before* the real trigger fires into a
  dead credential.

**Deliverables:** SDK probe field, preflight tool + cache table, cron
re-probe, promote-gate wiring. Probes for plugin-monday / plugin-browser /
slack land in their own repos as follow-ups.

---

## Phase 6 — UI: trust surface, manifest editing, run view (replay removed)

### Where each thing lives

**List (PlaybooksSection):** each card gets a compact trust row under the
existing `v3 · ran 2h ago · 3.4/day` line:
`✅ tests 4/4 · ✅ connections (2h ago) · 📝 intent reviewed v12` — each token
is red/amber/green and clickable (deep-links to the tab below). A playbook
with a pending candidate shows `candidate v13 awaiting promote`.

**Editor becomes tabbed** (PlaybookEditor): `Canvas | Code | Manifest |
Tests | Runs`.
- *Canvas* — unchanged react-flow view of the IR (live/candidate switch).
- *Code* — the Python source (read-only render of codegen output in v1;
  in-place user editing can come later — the agent is the primary writer).
- *Manifest* — **free-text markdown editor, user-editable directly**, save
  button; shows "last reviewed at vN" staleness note. This is the vision
  page for the playbook.
- *Tests* — spec list with last pass/fail per spec, run-all button, per-spec
  detail (assertion diff), preflight report (per-tool probe rows + re-probe
  button), and "pin run as test" entry point.
- *Runs* — see below.
- Persistent header strip across tabs: validation state + `Promote
  candidate` button (disabled with the failing gate named) + `Rollback`.

**Run view — replay removed.** Delete the timeline playback (`runReplay.ts`,
the canvas replay banner, the ▶ replay affordance in PlaybookRuns, and
StateVizPanel's frame-scrubbing mode). Replace with a **step execution
view**: a plain ordered list of `playbook_step_runs` — step id, kind, status,
duration, cost, retry count — each row expanding to resolved inputs, outputs,
error. Candidate test runs are badged. Where a spec was pinned from a run,
link back to it. (State history from `state` steps stays visible as a simple
table inside the expanded row — no scrubber.)

**Deliverables:** tabbed editor, trust rows, manifest editor + PUT route,
tests tab, new run view; removal of replay code paths.

---

## Phase 7 — cleanup & hardening

- Remove deprecated YAML input from propose/edit; codegen backfill for any
  playbook still without code.
- Round-trip + gate integration tests in `tests/`; dojo scenario: agent asked
  to change a playbook against its manifest → must refuse/ask (the
  gates-beat-prose proof for this feature).
- Docs: README tool table, manifest conventions, spec cookbook.

---

## Order & dependencies

```
1 Python layer ──► 2 manifest+staged flow ──► 3 candidate/promote ──► 4 specs ──► 5 probes ──► 6 UI ──► 7 cleanup
```

Strictly sequential at the top; 4 and 5 are internally parallelizable. UI
(6) intentionally last-but-one so it renders real data from 2–5, though the
run-view replacement (replay removal) has no dependency and can be pulled
forward if wanted.

## Backward compatibility & migration

The short version: **the IR never changes, so nothing breaks; code is a
derived view we backfill eagerly** (owner: few playbooks exist, migrating
them is fine — so we take the eager path and keep the compat window near
zero).

1. **Storage & execution: untouched.** `PlaybookDef` YAML stays the stored,
   validated, executed format. The runner, triggers, canvas, version history,
   and run history never learn about Python — an unmigrated playbook keeps
   running identically before, during, and after this plan ships.
2. **Code is derived-first, stored-after.** `codegen(IR)` can produce code
   for any existing playbook, so "has no code yet" is never an error state.
   Once a playbook is edited through the new flow, the authored code (with
   its comments) is stored on the version row; codegen remains the fallback
   for anything older.
3. **Eager one-shot migration** (instead of a long dual-input window): a
   migration pass at phase-1 rollout runs `codegen` over every playbook,
   verifies `compile(code) == IR` round-trip per playbook, and stores the
   code. Because codegen derives variable names *from existing step ids*,
   migrated playbooks keep their step ids exactly — run history, canvas
   node identity, and `steps.<id>` references all stay continuous.
4. **YAML input dies immediately after migration.** With all playbooks
   migrated and verified, `playbook_propose`/`playbook_edit` drop YAML input
   in the same release (no deprecation window needed — there is one agent
   and few playbooks). `playbook_get_definition` keeps an `ir=true` option
   for debugging.
5. **New columns are nullable and default-amber.** No manifest → the trust
   row shows "no intent written" (amber, not red) and the first edit's
   read stage asks for one; no specs → "untested" amber; probes never run →
   "unprobed" amber. The promote gate only hard-requires green on checks
   that *exist* (specs must pass if present; drift needs a manifest), plus
   static validation always. Given the eager stance, we backfill manifests
   for the existing playbooks by hand (owner + agent, minutes of work) right
   after phase 2 so nothing sits amber for long.
6. **Runs and rollback.** `playbook_runs.playbook_version` keeps pointing at
   version numbers; old versions without code render via codegen when
   inspected. Rollback to a pre-plan version is legal — it restores IR, and
   code regenerates.

## Risks / open questions

- **Step-id stability across code edits** — DECIDED (owner): id = variable
  name; renaming a variable is remove+add, same as a YAML id edit today.
  Codegen's id-preserving migration (point 3 above) means migration itself
  never triggers this.
- **Drift-check false positives** annoying the agent into force-asking the
  user. Mitigation: conflict threshold tuned to "clear contradiction only";
  the refusal message includes the manifest line it thinks is violated.
- **Compile subset too tight** (real playbooks need an expression we banned).
  Mitigation: expressions inside args are near-arbitrary; only inter-step
  control flow is restricted — same restriction the IR already imposes.
- **Ticket TTL vs long agent turns** — ticket survives the conversation turn,
  expires in hours not minutes.
- Version housekeeping before starting (found 2026-08-26 after pulling):
  local was 4 commits behind; origin/main is 0.7.0 but `luna-plugin.toml`
  still says 0.6.0 (three-stamps miss — in-code manifest is authoritative
  and correct), and the marketplace has only 0.5.1 — 0.6.0/0.7.0 were pushed
  but never published. Fix the toml stamp and publish 0.7.0 before phase 1
  lands on top of it.
