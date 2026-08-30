# Phase 6 — publish settings — execution summary

**In the repo, unreleased** (ships in 0.28.0 with phases 3–5 in phase 7).
Stamps stay at 0.27.1.

## What changed

Backend (`plugin_playbooks/`):

- `models.py`: `Playbook.publish_require_specs` and
  `Playbook.publish_require_run` (Boolean, not null, default True).
  `__init__.py`: two `_COLUMN_MIGRATIONS` entries
  (`BOOLEAN NOT NULL DEFAULT TRUE`) so existing rows keep the strict gates.
- `publish.py`: `specs_gate(..., require=True)` and
  `test_run_gate(..., require=True)`. With `require=False` the gate still
  runs and is reported (`ok: False`, `enforced: False`, note ends with
  "not enforced (Settings → Publish)") but returns no refusal. Every refusal
  string of both gates now ends with "Owner can relax this in Settings →
  Publish." (`error`, `message`, `hint`).
- `routes.py`: `_specs_gate_or_422` passes `require=p.publish_require_specs`;
  the promote (candidate + restore) and rollback `test_run_gate` calls pass
  `require=p.publish_require_run`. New
  `PATCH /playbooks/{name}/publish-settings` (`PublishSettingsPatch`:
  `require_specs?`, `require_run?`; 400 when both omitted, 404 unknown
  playbook) returning both flags. `GET /playbooks/{name}` returns
  `publish_require_specs` / `publish_require_run`.
- `agent_tools.py`: `_do_publish` passes the two flags to its gates
  (gates 2 and 3); `playbook_set_autonomy` gains optional boolean
  `require_specs` / `require_run` params, sets them, and reports the
  resulting flags. Static validation and preflight probes are untouched.

UI (`ui-src/src/playbooks/`):

- `PublishSettings.tsx` (new): `Switch` (Luna-style `w-10 h-5 rounded-full`,
  emerald when on, `role="switch"` + `aria-checked`) and the
  "Publish / Promote settings" section with the two rows "Pushing a
  version requires all tests to be green" and "Pushing a version requires
  at least one successful run", plus a footnote that validation/preflight
  are never switchable.
- `PlaybookEditor.tsx`: `publishSettings` state loaded from the playbook,
  `changePublishSettings` (optimistic, reverts on error) via
  `playbooksApi.patchPublishSettings`; rendered as `SettingsTab` children;
  `requireSpecs` passed to `VersionsTab`.
- `VersionsTab.tsx`: `requireSpecs` prop (default true) — the client-side
  "Promote to live disabled when red" only applies while the gate is on.
- `types.ts`, `api.ts`: the two flags on the playbook detail;
  `patchPublishSettings`.

Tests:

- `tests/test_publish_settings.py` (9): defaults + PATCH route + GET echo;
  tool sets the flags; restore with red specs refused (message names
  Settings → Publish) / allowed when the specs gate is off (spec result
  still cached red); tool publish reports the unenforced specs gate;
  restore without a run refused (error names the setting) / allowed when
  the run gate is off; rollback honours both flags; candidate publish via
  the tool with the run gate off reports `enforced: false` and goes live.
- `__tests__/PublishSettings.test.tsx` (2): both switches render from the
  value with the Luna classes; click reports only the flipped flag.
- `__tests__/VersionsTab.test.tsx` (+1): a red version's Promote is enabled
  when `requireSpecs` is false.

## Verification

- pytest **302 passed** (293 + 9); vitest **126 passed** (123 + 3);
  `tsc --noEmit` clean; `vite build` to a scratch dir OK.
- No real-environment step yet — phase 7 ships 0.28.0 and verifies in the
  browser (owner token needed).

## Deviations from PHASE.md

None.

## Reassessment of remaining phases

- Phase 7 (ship 0.28.0) unchanged in substance. Its checklist gains:
  confirm the two new `ALTER TABLE playbooks ADD COLUMN publish_require_*`
  log lines (alongside phase 5's `playbook_version` column, legacy index
  drop and spec backfill) on the local Luna; in the browser, flip a switch
  in Settings and confirm `Promote to live` on a red version becomes
  enabled and that the server then promotes it (gate reported, not
  refused).
