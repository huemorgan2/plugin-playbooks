# Playbook Validation Engine — research

**Problem.** A playbook can pass `playbook_validate` and `playbook_dry_run` and
still fail in the real world — or worse, silently do the wrong thing. The user
has no way to *feel* that a playbook actually does what it is supposed to do:
that its connections work, that its intent hasn't drifted after edits, and that
a change today didn't break a behavior that worked last month. This doc collects
the ideas raised (2026-08-26) plus design directions for a validation engine.

## What already exists (build on it, don't duplicate)

| Layer | Where | What it covers | What it can't tell you |
|---|---|---|---|
| Static validation | `validation.py::validate_definition` | schema, unknown keys, bad refs, cycles, unknown tools | nothing about runtime behavior |
| Dry run | `runner.py::dry_run` | real control flow, stubbed effectful leaves, arg-resolution trace | outputs are fake; connections untested; no assertions |
| Test fakes | `testing.py` (FakeAgent, fake tools, in-mem runner) | developer-level unit tests of the *engine* | not usable by the agent/user against *their* playbooks |

The validation engine is the missing top of this pyramid: **assertions +
fixtures + live probes + intent tracking**, exposed to the agent as tools and
to the user as visible green/red state.

---

## Idea 1 — Connection & access probes (live, side-effect-free)

Test that every tool a playbook uses is connected, authenticated, and has
access to the *specific resources* the playbook touches — without mutating
production data.

### The hard part, and ways to solve it

"Check you can write to the monday board without changing the board" has no
universal answer; it needs a **per-tool probe taxonomy**, roughly in order of
preference:

1. **Auth probe** — cheapest: call the API's identity endpoint (`me`,
   `whoami`, token introspection). Proves the credential is alive. Every
   connector has one.
2. **Resource read probe** — GET the exact resource the playbook writes to
   (board metadata, channel info, sheet properties). Proves the resource exists
   and the credential can *see* it. Args come from the playbook's resolved
   step args (the dry-run trace already computes these).
3. **Permission introspection** — some APIs report your role on a resource
   (monday `boards { permissions }`, Google Drive `capabilities`, GitHub
   `permission` on a repo). Proves *write* access without writing.
4. **API-native dry-run** — some APIs accept a validate-only flag
   (`dry_run=true`, `validate_only`). Use whenever available.
5. **Reversible write in a quarantined namespace** — when nothing above proves
   write access: write to a dedicated test container (a "Luna probe" item/group
   the probe creates, asserts, and deletes; or duplicate the board, test on the
   copy, delete the copy). Marked clearly as `mutating_probe`; requires user
   opt-in per connector.
6. **Write-then-revert** — last resort, never default. Only with an
   API-guaranteed revert (e.g. create+delete of an item that triggers no
   automations). Off unless explicitly enabled.

### Where probes live

- **Per-tool, declared by the tool's plugin** — add an optional `probe` recipe
  to `ToolDef` (or a plugin-level `probes` registry): a callable or a template
  describing "given these args, this read-only call proves access". The
  playbook engine shouldn't hardcode monday semantics; plugin-monday knows how
  to probe monday.
- **Fallback for undeclared tools**: auth probe on the tool's credential slot
  (vault entry exists + a generic ping if the client exposes one), and flag
  the tool as "connection verified, resource access unknown" — honest partial
  signal beats a fake green check.

### New tool: `playbook_preflight(name)`

Walks the definition, collects every `tool_call` (and agent-step `tools:`
allowlists), resolves static args (from the dry-run trace), runs each tool's
probe, and returns a per-tool report:

```
monday_update_board   ✅ credential ok · board 12345 visible · role: editor
slack_post_message    ✅ credential ok · #ops channel joined
gmail_send            ⚠️ credential ok · resource access not probeable
web_scrape            ❌ 401 from gateway — key missing
```

Run it: on demand ("test my playbook"), before first activation of a trigger,
and on a schedule (see Trust surface below).

### On "tests as code blocks that can call tools"

Extending inline-code-run blocks to call tools is powerful but a big security
and sandboxing step (tool calls from user-visible executable code = new
permission surface, approval-gate bypass risk). Recommendation: **don't** lead
with it. Declarative probes + fixture files (below) cover 90% of the need with
none of the new attack surface. Revisit only if declarative turns out too
rigid.

---

## Idea 2 — Intent manifest per playbook

A `manifest`/intent document attached to every playbook stating **why it
exists**: purpose, owner expectations, invariants, and acceptance criteria.
The agent must read it before editing and update it (or ask) when the change
conflicts.

### Shape (versioned with the playbook, one YAML/MD block)

```yaml
purpose: >
  Every Monday 9:00, summarize last week's Merkava leads from the monday board
  and post a digest to #sales. Never modifies the board.
triggers: [cron weekly Mon 09:00]
inputs: none
side_effects:
  - reads monday board 12345 (never writes)
  - posts exactly one message to slack #sales
invariants:
  - must not create/update/delete monday items
  - digest covers exactly the previous ISO week
  - runs under 2 minutes, no approval gates
acceptance:
  - given a week with 0 leads, still posts a "no leads" digest
  - given 200+ leads, digest stays under one slack message
```

### Enforcement (this is what makes it real)

- **Storage**: a column on `playbooks` + snapshot in `playbook_versions`, so
  intent history travels with definition history. Editable via
  `playbook_propose`/`playbook_edit` and the canvas UI.
- **Read gate**: `playbook_edit` returns the manifest alongside the YAML —
  the agent cannot fetch-for-edit without receiving intent in-context (same
  trick as stage-aware tool descriptions: put the context where the agent
  can't miss it, don't rely on prompt discipline).
- **Drift gate**: on save, a cheap LLM check — "does this diff conflict with
  any invariant?" Conflict → the save is refused with a steering hint telling
  the agent to either update the manifest (user-approved) or ask the user.
  This mirrors the flows-belong-in-the-tool-layer lesson: gates beat prose.
- **Staleness**: manifest untouched across N definition edits → surfaced as a
  warning in the trust panel ("intent last reviewed 12 edits ago").
- Machine-checkable invariants double as **simulation assertions** (below):
  "never writes to monday" is checkable in every dry-run trace — no monday
  tool_call in mutating mode; "exactly one slack message" is countable.

---

## Idea 3 — Simulation / unit tests ("playbook specs")

Fixture-driven test cases that run the playbook through the *existing* dry-run
machinery, but with **scripted stub outputs** instead of shape-derived
placeholders, and **assertions** on the trace. This subsumes idea 1's "run on
a test scenario" for logic (probes still cover connectivity).

### Test case format — YAML next to the playbook, stored like versions

```yaml
name: empty-week
inputs: {week: "2026-W34"}
stubs:                       # per-step or per-tool scripted outputs
  fetch_leads: {items: []}                # step id → output
  slack_post_message: {ok: true, ts: "1"} # tool name → every call
expect:
  status: done
  branch: {check_empty: then}             # condition took the `then` arm
  calls:
    - tool: slack_post_message
      args_contain: {channel: "#sales"}
      count: 1
    - tool: monday_update_item
      count: 0                            # invariant: read-only on monday
  output_contains: {digest: "no leads"}
```

### Engine changes needed

- `dry_run` grows a `stubs` param: stubbed leaf steps return the scripted
  value (falling back to today's shape-derived placeholder). Small change —
  the stubbing seam already exists (`ctx.dry`).
- An assertion evaluator over the returned trace + references namespace
  (branch taken, iteration counts, per-tool call count/args, final outputs).
  All data is already in the trace; this is a pure function.
- New tools: `playbook_test_add`, `playbook_test_run(name)` (all cases →
  pass/fail table), and a save-gate option: **edits don't save while specs
  fail** (or save with a loud degraded badge — user choice per playbook).

### Where fixtures come from (make it cheap, or it won't happen)

1. **Agent-authored at build time** — when the agent proposes a playbook it
   also proposes 2–3 cases (happy path, empty data, edge). The manifest's
   `acceptance` list is the natural seed.
2. **Record & replay** — the highest-value source: every *real* run's step
   outputs are already persisted in `playbook_step_runs`. "Pin this run as a
   test" converts a real run into a fixture (inputs + recorded tool outputs +
   assertions on what it did). Golden-trace regression for free, with real
   data shapes instead of invented ones.
3. **Mutation of recorded fixtures** — agent derives edge cases from a
   recorded one (empty the list, null a field, 10× the volume).

### LLM/agent steps in simulation

Two modes, per test case:
- **stubbed** (default): deterministic, free, fast — tests the control flow
  around the LLM.
- **live-judged** (opt-in, billed): run the real llm_step, assert with an
  LLM-judge rubric from the manifest's acceptance criteria. Use sparingly —
  nightly or pre-activation, not on every edit.

---

## Idea 4 — Agent behavior: the workflow is enforced by tools, not asked of the agent

The manifest and the tests only matter if the agent *actually* uses them on
every edit. Prompt instructions ("always read the manifest first") decay;
tool-layer gates don't (proven lesson: flows belong in the tool layer —
prose lost 5 dojo runs, gates won first try). So the edit workflow itself is
a **staged flow enforced by the tools**:

1. **Read stage** — `playbook_edit` / `playbook_propose`(update mode) do not
   accept new YAML on the first call. The first call *returns* the manifest +
   current definition + latest spec results, and a short-lived edit ticket.
   The agent literally cannot submit a change without having received intent
   in-context this turn.
2. **Write stage** — the save call requires the ticket. On save: static
   validation (exists) → drift check vs manifest → **specs auto-run** (stubbed
   mode, cheap and deterministic). The result lands in the tool response.
3. **Refusal with steering** — failing specs or a drift conflict refuse the
   save with a hint naming the failing case/invariant and the two legal moves:
   fix the definition, or (with user approval) update the manifest/specs.
   Tool descriptions are stage-aware, so even a cold agent discovers the flow
   from the tools themselves.
4. **No silent skips** — "tests didn't run" is a state the tools never allow;
   there is no save path that bypasses the spec run. An emergency escape
   (`force=true`) exists but requires an approval card, so skipping is a
   *user* decision, visible in the version history.

This costs one extra tool hop per edit — the price of never having to trust
agent discipline. The read stage also kills the context-loss failure mode:
after 40 turns of conversation the agent's working memory of "why this
playbook exists" is refreshed at exactly the moment it matters.

## Idea 5 — Test env: draft/candidate versions with promote & rollback (branch-like)

**What already exists (checked the code):** more than expected —

- Every save already snapshots the full definition into `playbook_versions`
  (append-only history with `author`, `message`, timestamps). The YAML-as-text
  intuition is right: a version is just a stored definition.
- **Rollback already exists**: `POST /playbooks/{name}/promote` copies any
  historical version back to live, recording `promoted_from` lineage. Version
  history is listable (`GET /playbooks/{name}/versions`).
- A `playbook_drafts` table + `POST /drafts/{draft_id}/promote` already gives
  a workspace that is *not* the live playbook (today used for canvas drafts).

**What's missing for real branch-like development:**

- **Agent-facing tools** — promote/rollback and draft-editing are REST/UI
  only; the agent's edit tools write straight to the live playbook. New tools:
  `playbook_draft_edit` (edit the candidate, not live), `playbook_promote`
  (candidate → live), `playbook_rollback` (live → previous version). All
  agent edits move to the draft path; **the live definition becomes something
  the agent can only reach via promote**, never write directly.
- **Testing a candidate** — dry-run, specs, and preflight must accept a
  version/draft ref, not just the live playbook. Runs get a
  `candidate: true` tag so trigger-driven production runs never pick up a
  draft. (Run rows already record `playbook_version`, so candidate run
  history slots in cleanly.)
- **Promote gate** — promote is where all validation converges: static ✅ +
  specs ✅ + preflight ✅ + manifest drift resolved, else refused (same
  steering-hint pattern as idea 4). This replaces "edit twice and hope" with
  forward (promote) / backward (rollback) — and rollback is instant and
  data-free because the old version is already stored.
- **One candidate per playbook, not N branches** — full git semantics (merge,
  diverging branches) buys little here and costs a lot of UI/mental load.
  Model: live version + at most one candidate + linear history. A second
  "branch" is a new playbook.

Net: this is mostly *wiring existing pieces* (versions table, promote route,
drafts table) into the agent tool surface plus a gate — not new machinery.

## Idea 6 — Code mode: the agent edits YAML, never items

**Status: already the design — verify the deployed version matches.** As of
006.714 the granular node tools (add/update/remove step, create draft, add
trigger, save) were removed from the agent surface exactly because they led
the agent to build playbooks piecemeal. Authoring is whole-YAML only:
`playbook_get_definition` → edit the full YAML → `playbook_edit`
(snapshot → validate → replace). If an agent is observed doing item-level
add/remove edits, it is running a pre-006.714 plugin version — check the
tenant's installed version before designing anything new.

**The remaining gap in code mode: whole-file rewrite doesn't scale.** For a
300-line playbook, forcing the agent to re-emit the entire YAML for a
one-line change is token-expensive and risks transcription drift (the agent
"fixes" things it wasn't asked to touch — the same reason code agents use
diff edits, not file rewrites). The middle ground is **text-level diff
editing**, not item-level tools:

- `playbook_edit` accepts either full YAML (today's mode) *or* an
  old-snippet → new-snippet replacement (the code-editor `Edit` pattern).
  It is still "editing code" — the unit is text, not steps — so it keeps
  the holistic-authoring benefit while dropping the rewrite cost.
- Validation always runs on the *resulting whole document*, so a diff edit
  can't dodge whole-playbook checks (refs, cycles, drift, specs).
- This composes cleanly with ideas 4/5: diff edits target the candidate
  draft, and the read-stage ticket already put the current YAML in context —
  exactly what the agent needs to write a correct snippet match.

**UI canvas stays item-level, agent stays code-level.** The react-flow canvas
patches (`playbook.patch` events) are for humans; they don't need to shape
the agent's tool surface.

## Idea 7 — Python authoring layer that compiles to the YAML IR (liked; phase-2 direction)

The "agent is mediocre at playbook YAML" problem is mostly a *language*
problem: models are heavily trained on Python and thinly trained on
YAML-with-Jinja DSLs. The proven pattern for fixing this without losing the
visual canvas is what Airflow / Dagster / Prefect / Kubeflow do: **write
Python, render a DAG** — by restricting the Python to a definition layer.

### Design

- **The current `PlaybookDef` (YAML) stays the canonical IR.** Stored,
  versioned, validated, rendered on the canvas, approval-gated — all
  unchanged. Python is an authoring *front-end* that compiles
  deterministically to the IR, never a second runtime.
- **Restricted definition layer.** `step()`-style calls register nodes;
  between-step control flow only via SDK combinators mapping 1:1 to existing
  step kinds (`branch()` → condition, `loop()` → loop, `approve()` →
  wait_for_approval, `run_playbook()` → subtask …). Raw `if`/`for` between
  steps is rejected; arbitrary expressions live only *inside* step args.
  This 1:1 mapping is what keeps the canvas: the code is isomorphic to the
  step tree.
- **Variables replace the template namespace** — the big LLM win:

  ```python
  leads = fetch_leads(board=12345)            # tool_call step
  digest = summarize(leads.items)             # llm_step
  post_slack(channel="#sales", text=digest)   # tool_call step
  ```

  Use-before-define becomes a NameError; the `steps.<id>.output` bug class
  (which validation.py special-cases today) is inexpressible; Jinja quoting
  hell disappears. Whole categories of validator checks vanish by
  construction instead of being caught after the fact.
- **Two-way**: IR → generated Python, so the agent can round-trip playbooks
  authored on the canvas or in YAML. Edits land as code, compile to IR, and
  every gate from ideas 2–5 (manifest drift, specs, promote) operates on the
  IR exactly as before.

### Safety constraint (non-negotiable)

Definition-pass code must be **side-effect-free and deterministic** — it
describes a graph, it doesn't act. Compile via AST parsing of the restricted
subset (preferred), or execute in a no-tools/no-network sandbox. Never run
agent-authored definition code with tool access; the compile step is not a
turn.

### Sequencing

Feedback-loop work (idea 6 / auto-validate in the edit tool) still comes
first — days not weeks, and it helps either syntax. This layer is the
phase-2 bet if YAML authoring still underperforms once the loop is tight:
front-end addition, not rewrite, because the IR and everything built on it
survives unchanged.

- **Trust surface in the UI** — the point of all this is user *feeling*. Each
  playbook card/canvas gets a validation panel: ✅ static · ✅ 4/4 specs ·
  ✅ connections (probed 2h ago) · ⚠️ intent stale. Green is earned, dated,
  and clickable (drill into the probe report / failing case). An unvalidated
  playbook says so, visibly.
- **Scheduled re-probing** — connections rot (expired tokens, revoked board
  access) while playbooks sleep. A daily preflight over all *active* playbooks;
  failures raise a chat notification *before* the Monday-9:00 trigger fires
  into a dead credential. (Reuses the existing cron trigger source.)
- **Shadow runs / canary** — for the riskiest playbooks: run the new version
  in dry-run against the *live* read-only data (probes fetch real inputs,
  writes stay stubbed), diff its would-be writes against the old version's.
  "Same inputs, new version would have posted X instead of Y" is the most
  convincing pre-deploy evidence there is.
- **Post-run verification steps** — invariants checked *after* real runs too:
  a lightweight `verify:` block in the definition (or derived from the
  manifest) that runs read-only checks after each run ("the digest message
  exists in #sales", "board item count unchanged") and marks the run
  `done+verified` vs `done-unverified`. Catches the failures simulation can't.
- **Failure-mode library per connector** — probes should distinguish
  *credential dead* / *resource gone* / *permission downgraded* / *rate
  limited*, because the fix differs. Plugins declare the mapping once.

## Suggested build order

1. **Draft/candidate flow + promote/rollback tools** (idea 5) — mostly wiring
   existing tables/routes into the agent tool surface; immediately stops
   editing production directly and gives instant rollback. The promote gate
   starts empty and each later layer plugs into it.
2. **Specs engine** (idea 3, stubbed mode + record-&-replay) — pure software,
   no new permission surface, immediately gives regression safety and the
   pass/fail table. Includes the `stubs` seam in `dry_run`. Runnable against
   candidates from day one.
3. **Intent manifest + staged edit flow** (ideas 2 + 4) — small schema change,
   large behavioral payoff; read-stage/write-stage gating with auto-run specs;
   the acceptance list feeds the specs.
4. **Preflight probes** (idea 1) — needs per-plugin probe recipes (start with
   auth + resource-read levels only; no mutating probes in v1) + the
   `playbook_preflight` tool; wired into the promote gate.
5. **Trust panel + scheduled re-probing** — the UI/UX layer that turns 1–4
   into user-visible confidence.
6. Later: shadow runs, post-run verification, live-judged specs.
