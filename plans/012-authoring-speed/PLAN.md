# 012 — authoring speed: cut the LLM round trips, not the tools

Status: PLANNED 2026-08-28. Phase 1 executes immediately (owner ask).

## Evidence — measured on a live tenant chat

Chat "playbook" on vaselin-scanny-2 (conversation `340e4304`, 2026-08-28,
112 messages), timed message-by-message via the tenant API. Findings:

- **Every tool call returned in 0.2–0.6s.** The runtime is not slow.
- Wall time is ~50 assistant inference round trips. The "write tests" turn
  alone took **~5.4 min across 29 LLM round trips**, of which:
  - 5 `playbook_spec_add` calls, strictly one spec per round trip — then,
    after a stub-shape discovery, **every spec re-updated one per round
    trip again** (5 more). ~10 round trips for what is one batch.
  - 4 round trips (~47s) fumbling to read the playbook code because the
    edit read stage returns it JSON-escaped — the agent literally said
    "It's all packed into one long line" and fell back to
    `playbook_get_definition`.
  - ~13 `tasks_set`/`tasks_update` bookkeeping round trips (~60–70s).
    That is luna-core prompt ceremony — fixed in **luna plans/088**, not
    here.
- Context bloat compounds per-step latency: the `playbook-authoring` skill
  is ~24KB, each `playbook_edit` read stage returns ~20.5KB (manifest +
  code + full `LANGUAGE_CHEATSHEET`), definitions ~11KB — re-entering
  context repeatedly.
- The real `monday_get_column_values` shape (`{"column_values": [...]}`)
  differed from the hand-written spec stubs (flat list). Dry-run passed,
  live run failed, and the whole spec suite had to be rewritten. The tool
  that prevents this — `playbook_spec_from_run` — exists but nothing
  steered the agent to it.

## Phases

### Phase 1 — batch spec operations (EXECUTES NOW)

`playbook_spec_add` gains a batch form: a new optional `specs=` parameter —
a YAML document `{spec-name: {spec body}, ...}` (map of specs keyed by
name). Exactly one of (`spec_name`+`spec_yaml`) or `specs` is accepted;
the old single form stays back-compatible. Each spec is parsed and
upserted; parse errors are collected per spec and do NOT abort the others.
After upserting, the full suite runs ONCE against the auto target and the
result returns per-spec pass/fail plus the suite summary — same shape the
agent otherwise assembles from N calls + a final `playbook_spec_run`.

Tool description steers: "add ALL the specs you intend to write in ONE
call via specs=". The `playbook-authoring` skill body's spec section gets
the same one-line steer.

Acceptance: unit tests for batch upsert (create + update mix), partial
parse failure, suite-run-once semantics; existing single-spec tests stay
green.

### Phase 2 — readable code payloads

The `playbook_edit` read stage and `playbook_get_definition` stop
embedding code inside a JSON string. New read-stage shape: a compact JSON
header (stage, editing, versions, ticket, expiry, instructions — no code,
no cheatsheet) followed by the manifest and the code as plain framed text
with real newlines:

```
{...header json...}
--- manifest ---
...
--- code (candidate v19) ---
...real multiline source...
--- end ---
```

The write stages keep returning JSON (small payloads, no code echo).

Acceptance: the read stage output contains raw newlines in the code block;
contract tests updated; a dojo-style probe confirms an agent can quote a
snippet from the read stage verbatim into `old=` without a re-read.

### Phase 3 — payload diet

- Read stage: replace the full `LANGUAGE_CHEATSHEET` (~8KB) with a ~15-line
  mini-reference (step kinds, ref shapes, the three rules agents actually
  forget) + the line "full reference: playbook_language_reference". The
  full cheatsheet stays one tool call away (plans/003 recall point is
  preserved — the recall POINTER survives compaction; the 8KB body no
  longer rides on every edit).
- Skill body audit: `_AUTHORING_SKILL_BODY` ~24KB → target ≤12KB. Move
  reference-grade detail (full YAML key tables, long examples) into
  `playbook_language_reference`; the skill keeps rules and workflow.

Acceptance: byte-size assertions in tests (read stage ≤6KB before code;
skill body ≤12KB); no lint/contract regressions.

### Phase 4 — real shapes first

- `playbook_status` on a **failed** run appends a steering hint: "capture
  this run's REAL tool outputs as spec stubs: playbook_spec_from_run(
  name=..., run_id=<this run>)".
- `playbook_spec_from_run` accepts failed runs (stubs pin every step that
  DID run; expectations default to the failure point) — today it only
  auto-picks `done` runs.
- Skill prose: "write stubs from recorded reality, not from memory —
  after any real run exists, start specs with playbook_spec_from_run."

Acceptance: unit tests for failed-run pinning; hint presence test.

## Non-goals

- The `tasks_set`/`tasks_update` ceremony — luna-core, see luna
  plans/088-batch-task-updates.
- A dedicated playbook sub-agent + progress UI — plans/013.
- Anything touching the runner, validation, or the language.

## Versioning

Phase 1 ships as **0.17.0** (three stamps: `pyproject.toml`, in-code
`PluginManifest.version`, README if it names a version). Later phases bump
minor again per repo convention.
