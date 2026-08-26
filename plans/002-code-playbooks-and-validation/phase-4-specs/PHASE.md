# Phase 4 — Specs engine (fixture simulation with assertions)

Scope doc written 2026-08-26, after phase 3 (0.10.0) shipped. Target version:
**0.11.0**.

## Goal

A playbook gets executable tests: fixture inputs + scripted tool outputs +
assertions over the dry-run trace. Specs run automatically on every candidate
save, on demand, and as a **promote gate** — an invalid candidate cannot go
live while a spec fails.

## Carried rules (from phases 2–3)

- Never insert a `playbook_versions` row at the current counter —
  ensure-live-row → bump → insert at the NEW number (spec runs snapshot
  nothing, so mostly moot, but any future spec-result-on-version write obeys
  it).
- Specs execute against the CANDIDATE via `_shim_playbook` — the runner
  stays untouched except the stubs seam below.
- New tools that mutate (spec add/delete) are skill-gated; running specs is
  read-only and needs no approval.

## Design

### 1. Stubs seam (runner)

`_RunContext` gains `stubs: dict[str, Any]` (default `{}`); `dry_run()`
gains `stubs=` and threads it in. In the existing `ctx.dry` branches:

- `_run_tool_call`: `override = ctx.stubs.get(step.id, ctx.stubs.get(step.tool))`
  — when present, `result` becomes the scripted value instead of
  `{"_dry": True}` (resolved args still surfaced; `"stubbed": True` marker).
- `_run_agent_step` / `_run_llm_step`: `ctx.stubs.get(step.id)` overrides
  `_stub_from_schema(...)`.

Step-id keys win over tool-name keys. Sub-workflow contexts inherit the
parent's stubs. That is the whole runner change.

### 2. Spec storage

New table `playbook_specs`:
`id UUID PK, playbook_id FK (cascade), name VARCHAR(128) (unique per
playbook), spec JSONB, created_by VARCHAR(64) ("agent"|"user"|"run:<id>"),
created_at, updated_at`. Registered in `_COLUMN_MIGRATIONS`-adjacent
create-all (new table — create_all covers it; no column migration needed).

Spec document (validated by a pydantic `SpecDef`):

```yaml
description: optional free text
inputs: {who: "Roy"}                # playbook inputs for the dry run
stubs:                              # step-id or tool-name → scripted output
  # the value IS the tool's result payload (no `result:` wrapper); for
  # agent/llm steps it replaces the whole step output
  fetch_orders: {orders: [{id: 1, total: 9}]}
expect:
  status: done                      # done|failed (default done)
  steps_ran: [fetch_orders, notify] # exact execution order (optional)
  steps_not_ran: [escalate]         # optional
  tool_calls:                       # optional, per tool
    send_chat_message:
      count: 1                      # exact count (optional)
      args_contain: {message: "Roy"} # substring/subset match on resolved args
  output_contains:                  # optional, substring match on a step's
    notify: "order"                 #   trace output (JSON-serialized)
  error_contains: "..."             # only with status: failed
```

### 3. Assertion evaluator

Pure function `evaluate_spec(spec: SpecDef, trace_result: dict) -> SpecResult`
in a new `specs.py` — no DB, no runner import. Checks against
`trace_result["trace"]` + `["status"]`/`["error"]`. `args_contain`: strings
match by substring, other values by equality; dicts are subset-matched
recursively. Result: `{name, passed, failures: [str], checked: int}` with
human-readable failure lines ("expected tool send_chat_message called 1x,
saw 0").

### 4. Tools (all chat_only)

- `playbook_spec_add(name, spec_name, spec_yaml)` — validate SpecDef, run it
  immediately against candidate-or-live, store, return the result (a spec
  that fails on arrival is stored but reported loudly). Upserts by
  (playbook, spec_name). Skill-gated, policy auto.
- `playbook_spec_list(name)` — specs + their last result (see cache below).
- `playbook_spec_delete(name, spec_name)` — skill-gated.
- `playbook_spec_run(name, spec_name=None, version="auto")` — run one or all
  specs against auto=candidate-else-live (explicit "live"/"candidate"/n as
  in dry_run); returns per-spec results + summary.
- `playbook_spec_from_run(name, run_id=None)` — record & replay: default
  latest completed run; builds stubs from `playbook_step_runs.outputs`
  (tool steps keyed by step_id), inputs from `playbook_runs.inputs`, expect
  seeded with `status` + `steps_ran` (execution order) + per-tool counts.
  Returns the spec YAML as a PROPOSAL for the agent to trim and save via
  `playbook_spec_add` — does not store directly (the agent should name it
  and strip noise).

Last-result cache: `playbook_specs.spec` sits beside `last_result JSONB` +
`last_run_at` + `last_version INT` columns on the same row — updated by
every evaluation, feeds `playbook_spec_list`, the promote gate report, and
phase 6 badges.

### 5. Save + promote integration

- **Auto-run on candidate save** (`_edit_impl` write path, after commit):
  run all specs against the new candidate (dry-run, so cheap). Results go in
  the save result as `specs: {passed: n, failed: m, failures: [...]}` — a
  failing spec does NOT block the save (it blocks PROMOTE); steering `next`
  mentions fixing or updating specs when failed.
- **Promote gate `specs`** in the existing gate list (after
  static_validation, before manifest_drift): re-run all specs against the
  candidate; any failure refuses promote naming the spec(s) and the failure
  lines. No specs → gate passes with note "no specs defined" (phase 6 will
  surface that as a trust gap, not an error).
- REST promote path gets the same gate (shared helper `run_specs_for(
  playbook, version_row|None)` in specs.py, called from both).

### 6. Skill body

New "SPECS (playbook tests)" section: what a spec is, the
add → auto-run-on-save → promote-gate loop, `playbook_spec_from_run` as the
preferred authoring path after a good live run, YAML shape reference.
CHANGING recipe gains "specs run automatically on save; fix or update them
before promote". Spec tools added to SkillDef.tools + AUTHORING_TOOLS
(mutating ones) — remember the phase-3 rule: every SkillDef tool must be in
AUTHORING_TOOLS.

## Out of scope

- Probes / connection checks (phase 5 — same gate list).
- Spec UI (phase 6 Tests tab renders `playbook_spec_list` + last results).
- Mutating stubs for real runs (stubs exist only in dry-run).

## Verification

- Unit (`tests/test_specs.py`): stub seam (step-id key, tool-name key,
  precedence, agent/llm stubs, sub-workflow inheritance), SpecDef
  validation errors, evaluator matrix (status, steps_ran order,
  steps_not_ran, tool count, args_contain subset + substring,
  output_contains, error_contains, readable failure lines), spec_add
  immediate run + upsert, spec_run all/one/version targeting, auto-run on
  candidate save (result embedded, save not blocked), promote refused on
  failing spec naming it, promote passes with no specs, spec_from_run
  builds stubs/inputs/expect from a recorded run, last-result cache
  updated, tool policies + gating.
- Real QA Luna: sync + restart; live turns — add a spec to qa-code-hello
  (from scratch), edit the playbook so the spec fails → save reports the
  failing spec → promote refused naming the `specs` gate → fix → promote
  passes; `playbook_spec_from_run` on the phase-3 run history produces a
  sane spec. DB checks after each step.
- Ship 0.11.0 (three stamps), commit, push, publish, catalog check,
  execution_summary.md + reassessment of phases 5–7.
