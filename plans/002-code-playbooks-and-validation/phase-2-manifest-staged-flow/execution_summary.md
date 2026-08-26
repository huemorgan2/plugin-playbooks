# Phase 2 — Manifest + staged edit flow: execution summary

**Status: DONE. Shipped as plugin-playbooks 0.9.0 (2026-08-26).**

## What was built

- **`manifest` columns** (`TEXT NOT NULL DEFAULT ''`) on `playbooks` and
  `playbook_versions` — free-text markdown intent (Purpose / Side effects /
  Never / Acceptance). Snapshot travels with versions; promote restores it.
  Migration via the generalized `_ensure_columns` (`_COLUMN_MIGRATIONS` list,
  replacing phase 1's code-only helper).
- **`playbook_edit_tickets` table** — single-use, 15-minute TTL, swept
  convergently on every issue (expired + used rows deleted). Each ticket pins
  `base_version`: a write against a playbook that changed since the read is
  refused with a re-read steer.
- **Staged `playbook_edit`** (flows-belong-in-tool-layer):
  - no payload → READ stage: `{stage, manifest, code, version, ticket,
    expires_in_seconds, instructions}` (+ `manifest_note` nudging
    `playbook_manifest_set` when empty).
  - payload without a valid ticket → refusal steering to the read stage.
  - write path: ticket check (not consumed) → compile → validate → **drift
    check** → re-check + consume ticket under `with_for_update` → snapshot →
    save. A compile error or drift refusal does NOT burn the ticket.
- **Drift check**: one `runner._agent.run_llm()` call (purpose
  `summarization`, output schema `{conflict: bool, reason: str}`), only when
  the manifest is non-empty. Conflict → refusal with the reason and three
  legal moves (fix code + same ticket / `playbook_manifest_set` /
  `playbook_edit_force`). LLM failure → fail OPEN, `drift_warning` in the
  result. No DB session or row lock is held across the LLM call.
- **`playbook_manifest_set`** (prompt_always) and **`playbook_edit_force`**
  (prompt_always, shared handler with `skip_drift=True`, override recorded in
  the version message). `playbook_propose` accepts `manifest=`.
- **Routes**: GET /playbooks/{name} returns `manifest`; PUT + promote
  snapshots carry it; promote restores it; new GET/PUT
  `/playbooks/{name}/manifest` (REST PUT is the owner — no approval gate,
  but it snapshots + bumps).
- **Skill**: new "MANIFEST + THE EDIT FLOW" section; THE LOOP and the
  CHANGING recipe rewritten for the two-step flow; `playbook_edit_force` and
  `playbook_manifest_set` added to the skill-gated tool list.

## Verification

- **88/88 tests** (`.venv/bin/python -m pytest tests/ -q`): 14 new in
  `test_manifest_flow.py` (read stage, ticket single-use/expiry/playbook-bound/
  sweep/compile-error-keeps-ticket/stale-version, drift called only with
  manifest + call shape asserted, conflict refusal + same-ticket retry,
  fail-open, force skips drift + records override, manifest_set snapshot+bump,
  snapshot carries manifest, prompt_always policies). Existing edit tests
  updated to fetch a ticket via the read stage; the no-payload "exactly one
  mode" assertion became a read-stage assertion.
- **Real QA Luna (:8766, luna_dev)** — migration added both manifest columns
  + tickets table on load; live agent turns:
  - (a) "set a manifest on qa-code-hello" → `playbook_manifest_set` approval
    card raised → approved via API → v3 with the manifest; the v2 snapshot
    correctly holds the PRE-change (empty) manifest.
  - (b) "greet in French" → agent did read stage (got manifest + ticket),
    write refused by the REAL LLM drift check quoting the manifest clause;
    playbook stayed v3, ticket not burned; agent reported the refusal and
    offered force/drop. (First attempt hit the known skill-gating hop —
    `playbook_edit` unlocks a turn after `load_skill`; `needs_continuation`
    flagged it and the follow-up turn ran the flow.)
  - (c) "more enthusiastic English greeting" → read stage → drift passed →
    v4 saved.
- Shipped 0.9.0 (three stamps), pushed, published to official, catalog
  verified.

## Surprises / decisions made during the phase

1. **Version-uniqueness invariant surfaced**: promote resolves snapshots with
   `scalar_one_or_none` by (playbook, version), so two snapshots may never
   share a version number. Manifest-set therefore BUMPS `playbook.version`
   (PLAN said "bumps nothing else"); every snapshot-then-write path now bumps.
2. **The ticket pins `base_version`.** The in-handler version guard only
   covered the LLM-call window; the real race is "playbook changed between
   read stage and write". Recording the version on the ticket at issue time
   closes the whole span and gives a clean convergent refusal.
3. **Tickets survive failed writes.** Compile errors and drift refusals keep
   the ticket valid — burning it forced a pointless re-read loop. Only a
   successful save consumes it.
4. **SQLite round-trips tz-aware datetimes as naive** — `_aware()` normalizes
   before TTL arithmetic (tests run on aiosqlite; prod is PG).
5. Deferred-tool-group hop (luna 046) reproduced exactly as memory says:
   first `playbook_edit` call in a fresh conversation returned an empty
   unknown-tool result; Luna's `needs_continuation` recovered on the next
   turn. No plugin-side action needed.

## Reassessment of phases 3–7

- **Phase 3 (candidate/promote)** — proceed, two carried rules: "code AND
  manifest travel with the definition, always" (promote already restores
  both), and "every snapshot bumps the version" (new invariant from this
  phase — candidate machinery must respect it). The edit-ticket seam is where
  candidate-vs-live editing should hook in (read stage can return which
  branch you are editing).
- **Phase 4 (specs)** — unchanged; the read-stage payload dict was built to
  be extended, so the spec summary slots straight into it.
- **Phase 5 (probes)** — unchanged.
- **Phase 6 (UI)** — additionally cheap now: GET /playbooks/{name} already
  returns `manifest`, and GET/PUT /manifest exist, so the manifest panel is
  pure frontend. Code tab still needs `code` added to the GET response.
- **Phase 7 (cleanup)** — unchanged (YAML removal + check_unknown_keys
  machinery removal + docs + dojo gates-beat-prose scenario). One addition:
  when YAML input is removed from edit, drop the `definition_yaml` branch of
  `_edit_impl` and the drift fallback `stored_code or definition_yaml`.

No re-ordering needed; dependencies hold.
