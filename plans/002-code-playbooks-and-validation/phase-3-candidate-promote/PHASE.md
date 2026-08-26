# Phase 3 — Candidate versions, promote, rollback

Scope doc written 2026-08-26, after phase 2 (0.9.0) shipped. Target version:
**0.10.0**.

## Goal

Editing stops mutating the running playbook. A save produces a **candidate**;
triggers and `playbook_run` keep executing the **live** version until the
candidate is **promoted** through a gate. Rollback restores a previous live.

## Semantics (the load-bearing decisions)

- `playbooks.definition` / `code` / `manifest` stay the LIVE content —
  runner, triggers, canvas, and every existing reader keep working untouched.
- Candidate content lives in `playbook_versions` rows. This REVERSES the
  historical meaning of that table for new rows: until now a row was a
  pre-change snapshot; from 0.10.0 a row holds the content OF that version
  number. Migration seeds a row for the current live state on first use so
  lineage is complete (idempotent, on_load).
- `playbooks.version` stays the monotonic counter (highest version number
  ever created). New columns: `live_version INT NOT NULL DEFAULT 0`
  (0 = "same as version", backfilled on load to `version`),
  `candidate_version INT NULL`.
- One candidate max: a new save overwrites `candidate_version` (the previous
  candidate row stays in history).
- The phase-2 edit flow: read stage returns the CANDIDATE code when one
  exists (you iterate on the candidate), else live; the write stage creates
  version row `version+1` with the NEW content and points
  `candidate_version` at it. Live untouched. Ticket base_version pins the
  counter as before.
- `playbook_propose` keeps creating live directly (a brand-new playbook has
  nothing to protect).

## Deliverables

1. Columns + `_COLUMN_MIGRATIONS` entries + on-load backfill
   (`live_version=version` where 0; seed missing live version rows).
2. Version-content helpers: `_load_version(session, playbook, n)` returning
   definition/code/manifest for version n (playbook itself when n is live).
3. Edit flow rework (`_edit_impl`): read stage reflects candidate; write
   stage creates the candidate row + sets `candidate_version`; result says
   `"status": "candidate_saved", "candidate_version": n, "live_version": m`
   and steers to dry_run → promote.
4. `playbook_promote(name)` — the GATE, extensible list: static validation of
   the candidate ✓, drift vs manifest resolved ✓ (candidate was saved through
   the drift gate or forced — recorded), specs/probes slots reserved
   (phases 4–5 plug in). Refusal names the failing gate. On pass: live
   content ← candidate row, `live_version=candidate_version`,
   `candidate_version=NULL`, events + canvas patch.
5. `playbook_rollback(name)` — live ← previous live version (walk
   `promoted_from` lineage / last version row before current live);
   snapshot-first, lineage recorded via `promoted_from`.
6. `playbook_dry_run(name, version='candidate'|'live'|n)` — default:
   candidate when one exists, else live (dry-running the thing you just
   edited is the point).
7. `playbook_run(name, candidate=true)` — supervised candidate test run,
   policy prompt_always, `playbook_runs.playbook_version` records the
   candidate version (tag visible in run history).
8. REST: GET /playbooks/{name} exposes `live_version`, `candidate_version`,
   and `candidate` summary; POST /{name}/promote absorbs the gate; new POST
   /{name}/rollback; versions list marks live/candidate.
9. Skill body: the candidate → dry_run → promote loop; promote/rollback
   tools registered (skill-gated where appropriate).

## Out of scope

- specs/probes gates (4–5) — the gate list is built to accept them.
- UI for candidate/live (phase 6).

## Verification

- Unit: full suite + new `test_candidate_flow.py` — save creates candidate
  and leaves live untouched; runner/trigger path executes live; dry_run
  defaults to candidate; promote gate refuses invalid candidate and names
  the gate; promote swaps content + clears candidate; rollback restores and
  records lineage; second save overwrites candidate; legacy playbooks
  backfilled (live_version seeded).
- Real QA Luna: sync + restart; live turns — edit qa-code-hello (expect
  candidate, live run still old behavior), dry-run candidate, promote, run
  (new behavior), rollback. Verify DB state at each step.
- Ship 0.10.0 (three stamps), commit, push, publish, catalog check,
  execution_summary.md + reassessment.
