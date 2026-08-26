# Phase 3 — Candidate / promote / rollback: execution summary

**Status: DONE. Shipped as plugin-playbooks 0.10.0 (2026-08-26).**

## What was built

- **Columns**: `playbooks.live_version` (`INTEGER NOT NULL DEFAULT 0`,
  0 = "same as version", backfilled on load via `backfill_live_version`) and
  `playbooks.candidate_version` (`INTEGER NULL`). `playbooks.version` stays
  the monotonic counter; `live_version` names the content that triggers and
  `playbook_run` execute; `candidate_version` points at the single
  un-promoted candidate row.
- **No migration seeding needed** — PHASE.md's "REVERSES the historical
  meaning" claim was wrong in a useful way: a pre-change snapshot of version
  n already IS the content of version n, so historical rows were always
  content-of-version-n. Only the CURRENT live version may lack a row on
  legacy playbooks; a lazy `_ensure_live_row` (author `system`, message
  "live content (recorded on first candidate/promote)") inserts it on first
  candidate save / promote / rollback / manifest set. Verified live: QA's
  qa-code-hello got its v4 row seeded exactly this way.
- **Edit flow rework** (`_edit_impl`): read stage returns the candidate code
  when one exists (`"editing": "candidate"|"live"`, plus `live_version` /
  `candidate_version`); write stage does ensure-live-row → `version += 1` →
  insert row with the NEW content → `candidate_version = version`. Live
  definition/code/manifest untouched. Result:
  `{"status": "candidate_saved", candidate_version, live_version, next: …}`.
  Emits `playbook.candidate.saved` (NOT `playbook.saved` — no trigger
  resync, no canvas patch; live did not change).
- **`playbook_promote`** — the extensible GATE (prompt_always, medium,
  skill-gated): `static_validation` (validate_definition on the candidate
  row) and `manifest_drift` (reported: resolved at save time, or
  "owner-approved forced edit" detected from the row message); specs/probes
  slots reserved for phases 4–5. Refusal names the failing gate. On pass:
  live ← candidate content (manifest stays live-owned), `row.promoted_from =
  old_live`, `live_version = candidate_version`, pointer cleared, NO counter
  bump. Emits `playbook.saved` + canvas patch.
- **`playbook_rollback`** — target = live row's `promoted_from`, falling
  back to the highest version row below live (legacy lineage). Restores
  definition + code + manifest. Same policy/gating as promote.
- **Candidate execution without touching the runner**: `_shim_playbook`
  builds a transient Playbook (never session-added,
  `version = live_version = row.version`) so `playbook_dry_run` (new
  `version="auto"|"candidate"|"live"|n`, default auto = candidate when one
  exists) and the new **`playbook_run_candidate`** (prompt_always, medium,
  skill-gated, trigger `agent-candidate`) execute candidate content through
  the unmodified runner. The only runner change is one line: `_create_run`
  stamps `playbook_version = live_version or version`.
- **`playbook_run`** on a playbook with a pending candidate appends a note
  ("this ran the LIVE version — an un-promoted candidate exists").
- **Manifest-set rewritten** (tool + REST PUT): ensure-live-row → change →
  bump → insert row for the NEW version → `live_version = version`. Same
  pattern for the owner PUT /playbooks/{name}. This is the fix for the
  version-collision bug below.
- **REST**: GET playbook returns `code` + `live_version` +
  `candidate_version` (list too); versions list flags `live` / `candidate` /
  `current` and only synthesizes an entry when live has no row; promote
  accepts `{version: null}` (candidate, gated, 422 on gate failure) or
  `{version: n}` (owner restore, brings that version's manifest back); new
  POST /{name}/rollback (409 without history).
- **Skill**: new "CANDIDATE → PROMOTE" section ("NEVER report an edit as
  done after `candidate_saved`"); CHANGING recipe ends
  validate → dry_run (candidate) → promote → run. Promote/rollback/
  run_candidate added to skill gating, and the phase-2 oversight fixed:
  `playbook_edit_force` + `playbook_manifest_set` were in SkillDef.tools but
  missing from AUTHORING_TOOLS, so 0.9.0 shipped them ungated.

## Verification

- **106/106 tests**: 18 new in `test_candidate_flow.py` (candidate save
  leaves live untouched + event assertions, read-stage candidate, iterate on
  candidate, dry_run auto/override/errors, promote swap + lineage + gate
  refusal (corrupted candidate names the gate) + keeps live manifest,
  rollback + no-history refusal, run_candidate shim + trigger tag, live-run
  pending-candidate note, manifest-set uniqueness regression, policies).
  12 existing tests updated: `"edited"` → `"candidate_saved"`, and
  single-row `scalar_one()` snapshot assertions became two-row (live seed +
  candidate) assertions.
- **Real QA Luna (:8766, luna_dev)**, five live agent turns on
  qa-code-hello (was v4 live, no version rows for v4):
  - (a) edit "mention the QA suite" → `candidate_saved`, DB
    `version=5, live_version=4, candidate_version=5`, v4 live row seeded by
    `_ensure_live_row`, live code unchanged. (Known skill-gating hop hit
    again — `needs_continuation`, one "continue" recovered.)
  - (b) dry-run → targeted the CANDIDATE (v5), correctly failed on the
    missing required input with the right step order.
  - (c) promote → `prompt_always` card ("Make a playbook's CANDIDATE version
    live") approved via API → gates reported (static_validation ✓,
    manifest_drift ✓ "checked at save time"), DB `5|5|NULL`,
    `promoted_from=4` recorded.
  - (d) live run with who=Roy → greeting mentions the QA suite; run row
    stamped `playbook_version=5`. (Agent first hit the autonomy gate —
    correct — and needed a nudge to file the `playbook_set_autonomy` card.)
  - (e) rollback → card approved → DB `5|4|NULL`, live code back to the
    pre-QA-suite greeting, v5 kept in history.
- Shipped 0.10.0 (three stamps), pushed, published to official, catalog
  verified.

## Surprises / decisions made during the phase

1. **History rows already held content-of-version-n** (see above) — killed
   the planned on-load seeding migration; lazy `_ensure_live_row` is smaller
   and self-healing.
2. **Version-collision bug caught before tests**: manifest-set snapshotting
   at `playbook.version` collides with a pending candidate that owns that
   number (live=3, candidate=4, counter=4). Rule extracted: **never insert a
   row at the current counter — always ensure-live-row, bump, insert at the
   NEW number, move `live_version`.** Both manifest paths and the owner PUT
   follow it; regression test asserts rows stay unique and the candidate
   survives a manifest change.
3. **Promote does NOT bump the counter** — it is a pointer move + content
   copy. Only content-creating writes bump. This keeps ticket
   `base_version` semantics from phase 2 intact (a candidate save bumps, so
   stale tickets still refuse).
4. **Manifest is live-owned**: candidate promote keeps the live manifest
   (candidates carry code changes, not intent changes); the owner
   restore-version path DOES bring that version's manifest back.
5. **`playbook_run(candidate=true)` became `playbook_run_candidate`** — a
   separate tool with its own prompt_always policy is clearer to gate and
   to steer than a boolean that flips risk semantics mid-tool.
6. Phase-2 gating oversight (edit_force/manifest_set ungated) found and
   fixed — worth a standing check: every tool in SkillDef.tools must also be
   in AUTHORING_TOOLS.

## Reassessment of phases 4–7

- **Phase 4 (specs)** — proceed as planned. The promote gate list is the
  insertion point (`static_validation` shows the shape: name + ok + issues +
  hint). Spec runs execute against the CANDIDATE via `_shim_playbook` — no
  new runner machinery needed. The read-stage payload gets the spec summary.
  Carried rule 2 applies to any spec-result snapshotting.
- **Phase 5 (probes)** — unchanged; probes plug into the same gate list.
- **Phase 6 (UI)** — cheaper again: GET playbook now returns `code`,
  `live_version`, `candidate_version`, and the versions list flags
  live/candidate, so the candidate banner + version timeline are pure
  frontend. Run history can badge candidate runs via trigger
  `agent-candidate` + `playbook_version`.
- **Phase 7 (cleanup)** — unchanged, plus: drop `_load_version`-era duplicate
  helpers if any remain after 4–5, and consider surfacing the stale pending
  approvals sweep (QA DB still holds July's pending cards — TTL is null).

No re-ordering needed; dependencies hold.
