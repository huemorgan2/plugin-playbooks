# Phase 6 — publish settings (owner-switchable gates)

## Baseline (2026-08-30, after phase 5)

- plugin-playbooks `e932a9d`. pytest 293, vitest 123, tsc clean.

## Scope

PLAN.md Part C2, adjusted by the phase-5 reassessment: `specs_gate` and
`test_run_gate` are already the single entry points in all three publish
paths (routes promote — candidate + restore —, routes rollback, tool
`_do_publish`), so each flag is one keyword at each call site.

### Backend

- `Playbook.publish_require_specs`, `Playbook.publish_require_run`
  (Boolean, default True) + two `_COLUMN_MIGRATIONS` entries
  (`BOOLEAN NOT NULL DEFAULT TRUE`).
- `specs_gate(..., require=True)`: when `require` is False a red result is
  still run and reported (`ok: False`, `enforced: False`, note says
  "not enforced — Settings → Publish") but the refusal is None.
  `test_run_gate(..., require=True)`: same shape — a missing/failed run is
  reported, never refused, when off. Static validation and probes stay
  unswitchable (untouched).
- Refusal text for both gates ends with
  "Owner can relax this in Settings → Publish." (error + message + hint).
- `PATCH /playbooks/{name}/publish-settings` body
  `{require_specs?: bool, require_run?: bool}` → returns both flags.
  `GET /playbooks/{name}` returns `publish_require_specs`,
  `publish_require_run`.
- Tool `playbook_set_autonomy` gains optional `require_specs`,
  `require_run` booleans (same semantics), reported in its result.

### UI

- `api.patchPublishSettings`, types on the playbook detail.
- New `PublishSettings.tsx`: section "Publish / Promote settings" with two
  Luna-style switches (`w-10 h-5 rounded-full`, knob `left-[22px]` /
  `left-0.5`): "Pushing a version requires all tests to be green" and
  "Pushing a version requires at least one successful run". Rendered as
  `SettingsTab` children; optimistic toggle, reverts on error.
- `VersionsTab` gets `requireSpecs` — the client-side "disabled when red"
  on `Promote to live` only applies when the gate is on.

### Tests

- pytest `tests/test_publish_settings.py`: PATCH route + GET echo;
  candidate promote with red spec refused when on / allowed when off
  (gate still reported red in the tool's result); restore with no run
  refused when on / allowed when off; tool `playbook_set_autonomy` sets
  the flags; refusal text names Settings → Publish.
- vitest `PublishSettings.test.tsx`: renders both switches from props,
  click → `patchPublishSettings` with the flipped flag; `VersionsTab`:
  red specs + `requireSpecs=false` → Promote enabled.

## Verification

tsc, vitest, pytest green; `vite build` OK. Browser verification is
phase 7's (needs an owner token).
