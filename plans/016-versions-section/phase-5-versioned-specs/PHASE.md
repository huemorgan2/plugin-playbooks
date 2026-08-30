# Phase 5 — specs travel with versions (duplicate-on-mint)

## Baseline (2026-08-30, after phase 4)

- plugin-playbooks `699776e`. pytest 284, vitest 121, tsc clean.

## Scope (Part C1 of PLAN.md)

### Schema

- `PlaybookSpec.playbook_version: int NOT NULL DEFAULT 0` (0 = "not yet
  backfilled"). Unique index becomes
  `ix_playbook_specs_playbook_version_name (playbook_id, playbook_version, name)`.
  The legacy unique index `ix_playbook_specs_playbook_name` is dropped on
  load (`_LEGACY_INDEXES`) before the new one is created, else duplicates
  would be refused.
- `backfill_spec_versions(session_factory)` on load: rows at 0 → the
  playbook's live version; if a candidate exists, copy them to it as well
  (fresh `last_*`). Idempotent.

### One mint helper (`plugin_playbooks/versioning.py`, new)

- `mint_version(session, p, *, definition, code, manifest, author, message,
  source_version, promoted_from=None) -> PlaybookVersion` — increments
  `p.version`, adds the row, and copies every spec of `source_version` to
  the new number with `last_result/last_run_at/last_version = None`.
  Replaces the four `version += 1` sites: `routes.py` PUT definition
  (source = live) and manifest save (live); `agent_tools.py` candidate
  save (source = candidate if one exists, else live) and manifest set
  (live). `ensure_live_row` moves here too (both copies delegate) — it
  creates a row for an existing number, so no spec copy.
- `copy_specs(session, playbook_id, from_version, to_version)` exported for
  the backfill.

### Reads / writes are versioned

- `specs.run_all_specs(..., version_n)` filters
  `playbook_version == version_n` — every existing caller already passes
  the version it runs against, so the semantics fall out.
- Tools: `playbook_spec_add`, `playbook_spec_delete`, `playbook_spec_list`
  gain `version: str = "auto"` (auto = candidate when one exists, else
  live; `live` / `candidate` / number) resolved via `_spec_target`; writes
  land on that version's set. `playbook_spec_from_run` stores on the run's
  version's set when that version still exists, else auto.
- Routes: `GET /specs?version=N` (default = candidate else live; response
  carries `version`), `POST /specs/run?version=N` (same default; explicit
  N runs N's specs against N's content). `GET /versions` rows gain
  `specs: {total, failed}` from that version's cache. `_trust_summaries`
  (list badges) counts only the candidate-else-live set per playbook.

### Gates read the target version's specs

- New `publish.specs_gate(session, runner, playbook_id, target, version_n)`
  → `(gate_dict, refusal_dict | None)`. Used by `promote_version`,
  `rollback_playbook` (routes) and `_do_publish` (tool) for candidates
  **and restores** — supersedes plan 015 deviation #4 ("restores skip
  specs"). Restoring v8 runs v8's specs against v8's content. A version
  with no specs passes with note "no specs defined". Phase 6 makes the
  refusal switchable.

### UI

- `TestsTab` takes `version: number`; header "Tests of vN"; `getSpecs` /
  `runSpecs` pass `?version=N`. `VersionsTab` passes the selected version.
- Version rows show `N tests · N green` (or `N red`) when the version has
  specs; the toolbar Promote button is disabled with a title reason when
  the selected version's cached specs are red.

## Tests

- pytest `tests/test_versioned_specs.py`: candidate save duplicates specs
  with reset results; spec added on the candidate does not touch live's
  set; owner PUT (live mint) duplicates; restore of v1 evaluates v1's specs
  only (v1 green / v2 red → promote v1 OK; v1 red → 422 `gate: specs`);
  `GET /specs?version=`; backfill (rows at 0 → live, copied to candidate;
  idempotent); `GET /versions` carries `specs`.
- vitest `TestsTab` header + versioned api call.

## Verification

tsc, vitest, pytest green; `vite build` OK. Real-env: phase 7.
