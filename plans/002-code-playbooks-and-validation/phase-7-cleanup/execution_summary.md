# Phase 7 — cleanup & hardening — execution summary

Shipped as **0.14.0** (ui-src untouched, stays 0.3.0). 148 pytest green.
Final phase of plans/002 — the plan is complete.

## 1. YAML input removed from propose/edit
- `playbook_propose`: `code=` is the only authoring input. `definition_yaml`
  survives as a declared-nowhere kwarg that returns a steering hint ("YAML
  authoring was removed — write the playbook as `code`"), never a TypeError.
  No payload at all → "Provide 'code' — the full playbook source."
- `playbook_edit` / `playbook_edit_force`: write stage takes exactly one of
  `code=` or `old=`/`new=`; a `definition_yaml=` call gets the hint
  ("pass code= or old=/new="). Schemas and descriptions cleaned.
- `parse_yaml` import dropped from agent_tools.py. `playbook_validate`
  deliberately KEEPS `definition_yaml` as a checker input, and
  `playbook_get_definition(format='yaml')` keeps showing the compiled IR —
  read/check surfaces, not authoring.
- Authoring skill body updated (edit-flow wording, legacy note replaced,
  trigger note now says `triggers=[trigger(...)]` in code, and the
  `playbook_list_available_triggers` description matches).
- Backfill: `backfill_code` already runs on load (phase 1); DB-verified on QA
  that zero playbooks have empty `code`.
- Left alone on purpose: REST `POST/PUT /playbooks` still accept
  `definition_yaml` (legacy human/API surface, not the agent authoring path;
  the UI never authors code).
- Tests: the two YAML-path tests rewritten to pin the new behavior
  (`test_propose_requires_code_and_refuses_yaml`,
  `test_yaml_edit_is_refused_with_steering_hint`).

## 2. Dojo manifest-refusal scenario — PASSED
See `dojo-manifest-refusal.md` + the three `.sse` transcripts in this folder.
Two variants, both refused the against-manifest change: with the manifest in
conversation context (refusal with zero tool calls) and in a fresh
conversation, where the ticketed read stage forced the manifest read and the
agent routed to `playbook_manifest_set` → owner-approval card (rejected to
close). DB probes: version/live/candidate and version-row count unchanged, no
"hola" in any stored code.

## 3. Step-output normalization
`_normalize_tool_result` in runner.py: tool-handler results that are JSON
strings parsing to a dict/list are stored parsed; plain text passes through.
Applied at the single `_run_tool_call` return point, so step outputs,
`spec_from_run` stubs, and the Runs tab all see structured data. New test in
test_async_run.py; verified live on QA — `send_chat_message` now records
`{"status": "sent", "message_id": ..., "conversation_id": ...}` instead of a
quoted JSON string.

## 4. Docs
README rewritten: code-first description, full tool table (4 always-visible +
18 skill-gated), manifest conventions, and a spec cookbook (stub semantics,
`args_contain` matching rules, happy-path + failure-branch examples,
`spec_from_run` note).

## 5. QA leftovers pruned
- Archived: qa-p6-glow, phase0-hello, phase0-hello-mrb71nhp, and the three
  phase2-action-* playbooks (DELETE is archive-by-design — hidden and inert).
- qa-code-hello manifest: "(edited during phase-6 browser QA)" line removed
  (now v11).
- Scratch Chrome profile deleted from the scratchpad.
- The list on QA now shows only qa-code-hello.

## Verification
- 148 pytest green; grep proves the only `definition_yaml` references left in
  agent_tools.py are the steering-hint kwargs and playbook_validate.
- QA Luna :8766 serving 0.14.0; live end-to-end run of qa-code-hello done
  with normalized outputs.

## Plan 002 wrap-up
Phases 0–7 all executed and shipped (0.8.0 → 0.14.0): pblang code authoring +
compiler + codegen/backfill, manifest + ticketed edit flow, candidate/promote
with gates, specs, probes/preflight, trust-surface UI with tabbed editor and
run view, and this cleanup. Remaining known debt: REST create/update still
YAML (legacy), and installed agents must be manually upgraded from the
marketplace. ProbeDef shipped upstream as luna 0.84.002 (2cacd43).

## Retest on luna 0.84.002 + latest plugins (2026-08-26)

The original QA above ran on a stale 0.34.x luna. Everything was re-verified
on a fresh environment: luna 0.84.002 (origin/main worktree, fresh DB
`luna_qa084` migrated to head) with the latest marketplace set — playbooks
0.14.0, chat-ui 0.13.0, files 0.11.0, mcp 0.2.0, connectors 0.1.4,
interview 0.3.0, monday 0.3.0, recall 0.1.2 — all via `build_plugin_set.py`
(sha256-verified artifacts, `LUNA_PLUGIN_SET_DIR`). **All green, no code
change needed; 0.14.0 stands.**

- Code propose + manifest: `qa84-hello` created from `code=`, manifest stored.
- Specs: add ran the spec immediately (passed, 3 checks); solo `spec_run` 1/1.
- Dry-run: correct trace + resolved args.
- Edit ticket flow: read stage → `old=`/`new=` write → candidate v2; promote
  behind owner-approval card; approved → live v2; live run recorded
  **normalized structured step outputs** (`{"status": "sent", ...}` as dict).
- Run autonomy: `playbook_run` under `agent_must_confirm` refused with the
  steering hint → `playbook_set_autonomy` approval card → approved → runs done.
- **ProbeDef first real integration** (core finally has the field): a managed
  fixture plugin declared `ProbeDef(kind="auth", handler=...)` on two tools —
  preflight returned `ok` / `failed(credential_dead)` exactly, and
  `playbook_promote` was **blocked by the probes gate even after owner
  approval**, with the failing tool + hint in the error.
- Dojo manifest-refusal (fresh conversation): ticketed read surfaced the
  manifest; the drift check refused the Spanish edit with correct reasoning;
  the agent escalated to `playbook_edit_force`, which parked on an owner
  approval (rejected). DB probes: 2 version rows, zero contain "hola".

Environment notes (not plugin defects): 0.84 executes an agent's batched tool
calls concurrently, so `spec_add`+`spec_run` in one batch race (run saw 0
specs; solo run passed) — steering-hint candidate for later. Dead SSE turns
plus queued messages re-fired `playbook_run` (3 duplicate runs). Old QA env's
`LUNA_DB_POOL=1` breaks 0.84 (asyncpg single-connection contention) — drop it.
