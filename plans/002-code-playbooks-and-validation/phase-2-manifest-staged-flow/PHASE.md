# Phase 2 — Manifest (free text) + staged edit flow

Scope doc written 2026-08-26, after phase 1 (0.8.0) shipped. Target version:
**0.9.0**.

## Goal

Every playbook gets a free-text **manifest** (its intent: purpose, side
effects, invariants, acceptance — plain markdown, no schema), and editing
becomes a **two-stage flow with gates**: read (manifest + code + ticket) →
write (ticket required, compile, validate, LLM drift-check against the
manifest). Gates in the tool layer, not prose (memory:
flows-belong-in-tool-layer).

## Deliverables

1. **Columns + migration** (same pattern as phase 1's `code`):
   - `playbooks.manifest TEXT NOT NULL DEFAULT ''`
   - `playbook_versions.manifest TEXT NOT NULL DEFAULT ''` (snapshot travels
     with history; `_snapshot_version` carries it; promote restores it)
   - on-load `ALTER TABLE` via `_ensure_columns` (generalize phase 1's
     `_ensure_code_columns`).
2. **`playbook_edit_tickets` table** — id UUID, playbook_id FK, created_at,
   used_at nullable. TTL 15 minutes; single-use. Expired/used rows swept
   convergently on ticket creation (memory: prompt-invariants-need-code-
   reapers — code cleanup, not trust).
3. **Tool changes** (`agent_tools.py`):
   - `playbook_edit(name)` with NO payload → **read stage**: returns
     `{manifest, code, version, ticket, expires_in}` plus stage instructions.
   - `playbook_edit(name, ticket, code=|old=/new=|definition_yaml=)` →
     **write stage**. No/invalid/expired ticket → refusal that steers to the
     read stage (never silently proceeds). Then compile → validate →
     **drift check** → snapshot (with manifest) → save (unchanged from 0.8.0
     after the gate).
   - **Drift check**: only when `manifest` is non-empty and the agent seam
     exists — one `runner._agent.run_llm()` call, purpose `summarization`,
     output schema `{conflict: bool, reason: str}`, prompt = manifest + old
     code + new code. `conflict=true` → refuse with the reason and the two
     legal moves (change the code, or `playbook_manifest_set` — which raises
     an owner approval card). LLM failure → **fail open** with a recorded
     warning (an outage must not brick editing).
   - `playbook_manifest_set(name, manifest)` — new tool, policy
     `prompt_always` (owner approval card via existing machinery). Bumps
     nothing else; records a version snapshot ("manifest updated").
   - `playbook_edit_force(name, ticket, ...)` — same handler with the drift
     gate skipped, policy `prompt_always` (the PLAN's "force raises an
     approval card", implemented with the per-tool policy we already have).
     Version message records the override.
   - `playbook_propose` accepts optional `manifest=` (encouraged in skill).
   - `playbook_get_definition` stays ticket-free (pure read).
4. **Routes**: PUT /playbooks/{name} and promote carry `manifest` like `code`.
   New `GET/PUT .../playbooks/{name}/manifest` for the phase-6 UI.
5. **Skill update** (`_AUTHORING_SKILL_BODY`): the staged flow recipe (read →
   ticket → write), manifest conventions (Purpose / Side effects / Never /
   Acceptance), drift-refusal handling. New code examples (if any) added to
   `test_skill_examples_compile`.

## Out of scope

- candidate/live versions, promote gates (phase 3)
- specs, probes (phases 4–5), UI (phase 6)
- spec/probe summary in the read stage payload (phases 4–5 will splice it in;
  the read-stage dict is built to be extended)

## Verification

- Unit: full suite green + new tests — ticket lifecycle (issue, single-use,
  expiry, sweep), read stage payload, write refused without ticket, drift
  check called only with manifest (fake agent stub asserting the call),
  conflict refusal shape, fail-open on LLM error, manifest snapshot/promote
  restore, manifest_set approval policy.
- Real QA Luna (:8766): sync + restart; live agent turns — (a) set a manifest
  on qa-code-hello via approval flow, (b) ask for an edit that violates the
  manifest → agent must hit the drift refusal and report it, (c) a compliant
  edit passes through the two stages.
- Ship 0.9.0 (three stamps), commit, push, publish, catalog check.
