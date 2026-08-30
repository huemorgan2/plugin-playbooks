# Phase 5 — specs travel with versions — execution summary

**In the repo, unreleased** (ships in 0.28.0 with phase 6). Stamps stay at
0.27.1. Part C1 of the plan: tests belong to the version they were written
against, duplicated on every new version, so an old version can be restored
under an "all tests green" gate.

## What changed

### Schema / load

- `PlaybookSpec.playbook_version` (INTEGER NOT NULL DEFAULT 0) added to
  `models.py` and to `_COLUMN_MIGRATIONS`. Unique index is now
  `ix_playbook_specs_playbook_version_name (playbook_id, playbook_version,
  name)`. The old `ix_playbook_specs_playbook_name` is dropped at load by
  the new `_drop_legacy_indexes` step (`_LEGACY_INDEXES`) so duplicate
  names across versions are allowed; the model's index loop then creates
  the new one.
- `backfill_spec_versions(session_factory)` runs after the live_version
  backfill: rows at 0 are pinned to the playbook's live version; if a
  candidate exists it gets its own copy. Idempotent.

### One mint helper — `plugin_playbooks/versioning.py` (new)

- `mint_version(session, p, *, definition, code, manifest, author, message,
  source_version, promoted_from=None)` increments `p.version`, adds the
  `PlaybookVersion` row and `copy_specs(source_version → new)` with
  `last_result/last_run_at/last_version = None`. All four ad-hoc
  `version += 1` sites now call it: `routes.py` PUT definition and manifest
  save (source = live), `agent_tools.py` candidate save (source = previous
  candidate if any, else live — `spec_source_version`) and manifest set
  (live). `ensure_live_row` / `live_version_of` / `get_version_row` live
  here too; the two `_ensure_live_row` copies delegate.

### Reads / writes / gates

- `specs.run_all_specs` filters `playbook_version == version_n`; every
  caller already passed the version it runs against.
- Tools `playbook_spec_add`, `playbook_spec_list`, `playbook_spec_delete`
  take `version: 'auto' | 'candidate' | 'live' | N` (auto = candidate when
  one exists, else live) — schemas updated; list/delete responses carry
  `version`.
- Routes: `GET /specs?version=N` (response has `version`; default
  candidate-else-live), `POST /specs/run?version=N` (runs N's specs against
  N's content; 404 for unknown N). `GET /versions` rows carry
  `specs: {total, failed, green}`. `_trust_summaries` (list badges) counts
  only each playbook's candidate-else-live set.
- `publish.specs_gate(session, runner, playbook_id, target, version_n)` →
  `(gate_entry, refusal | None)`; refusal text names the version
  ("… 2 of 2 red on v2"). Used by `promote_version` (candidate **and**
  restore), `rollback_playbook` and `_do_publish` — restores are no longer
  exempt (supersedes plans/015 deviation #4). A version with no specs
  passes with note "no specs defined".

### UI

- `TestsTab` takes `version`; header `Tests of vN`; `getSpecs` / `runSpecs`
  send `?version=N`. `VersionsTab` passes the selected version and renders
  `N tests · N green` / `N red` / `… not run` on each row
  (`version-specs-N`); Promote is disabled with a title reason while the
  selected version's cached specs are red.

### Tests

- `tests/test_versioned_specs.py` (9): candidate save duplicates live's
  specs (distinct rows, own caches); `mint_version` resets the copied cache
  and `copy_specs` is idempotent; a spec added/deleted on the candidate
  leaves live's set alone; owner PUT and manifest save mint with specs;
  `GET /specs` default/`?version=`, `POST /specs/run?version=`, `GET
  /versions` spec counts; restore of v1 runs v1's own (green) spec while v2
  (red) is refused with `gate: specs` naming v2 and both failing specs;
  `playbook_publish` restore uses the same gate; rollback reads the
  target's specs; backfill pins legacy rows and copies to the candidate,
  idempotent.
- vitest: `TestsTab.test.tsx` (1) and one more `VersionsTab` case (row
  counts + disabled Promote on red).

## Verification

- pytest **293 passed** (284 + 9). vitest **123 passed** (121 + 2). `tsc`
  clean. `vite build` to a scratch dir OK.
- Real-environment check deferred to phase 7 (same token blocker as
  phase 1). The schema migration (column ALTER + legacy index drop +
  backfill) will be exercised there against the local Postgres that still
  holds 0.27.1 data.

## Deviations from PHASE.md

- `GET /versions` `specs` also carries `green` (needed for "N green" vs
  "not run" wording).
- `playbook_spec_from_run` untouched — it only proposes a spec document,
  it never stores one.
- The manifest-save mint on the tool side reads `old_live` before
  changing `manifest` (was implicit before).

## Surprises / learnings

- The existing 284 tests passed unchanged after the backend switch — the
  candidate auto-run and the `playbook_spec_run version=` tests already
  addressed one version at a time, so per-version filtering was
  behaviour-preserving for them.
- `PUT /playbooks/{name}` takes `definition_yaml`, not a definition dict
  (cost one test-fixture round).

## Reassessment of remaining phases

- Phase 6 (publish settings): `specs_gate` is now the single specs entry
  point in all three publish paths and returns `(gate, refusal)`, so the
  `require_specs` flag is a one-line decision at each call site
  ("report, don't refuse" when off). The run gate is already
  `test_run_gate` in the same three places. Phase 6 scope is otherwise
  unchanged: two columns (`publish_require_specs`, `publish_require_run`,
  default true) + `_COLUMN_MIGRATIONS`, `PATCH /publish-settings`, flags
  on `GET /playbooks/{name}` and the `playbook_set_autonomy` tool, the two
  switches as `SettingsTab` children, and refusal text that names the
  setting. The UI's client-side "disabled when red" on Promote must also
  honour `require_specs` (only disable when the flag is on).
- Phase 7 unchanged; add to its checklist: confirm on the local Postgres
  that the legacy spec index was dropped and rows were backfilled (log
  lines "dropped legacy index" / "versioned N spec row(s)").
