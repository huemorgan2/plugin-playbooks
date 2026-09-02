# Phase 4 — E2E, regression sweep, ship 0.34.0

## Scope

1. **Regression sweep** — full pytest suite; UI toolchain (`vitest`, `tsc
   -b && vite build`) since the artifact ships `ui/`; `test_ops_exceptions
   .py` explicitly green (hard constraint from the brief).
2. **Version 0.34.0** — all three stamps (`plugin_playbooks/__init__.py`,
   `luna-plugin.toml`, `pyproject.toml`; pinned by
   `test_version_stamps_agree`), commit, push origin main.
3. **Publish** — `scripts/package_plugin.py` → `publish_plugin.sh` to
   `official` (LUNA_MP_TOKEN2), verify catalog `latest_version == 0.34.0`,
   swap the managed QA copy to the released code and confirm a clean load.
4. **E2E** — as far as this session can drive it: every server-side
   surface exercised for real on QA Luna (done in phases 1–3: table,
   card route no-oracle, authed API, card in a sandboxed iframe with a
   live running→done transition). The one step that requires a human in
   chat — a fresh conversation invoking `playbook-delegation` →
   `playbook_agent` → watching the card → approving the publish gate — is
   written up as a 5-minute owner script in the execution summary.

## Verification criteria

- Suite green on the exact release commit; UI build clean.
- Catalog shows 0.34.0; QA Luna loads the released code cleanly with both
  delegation tools registered.

## Out of scope

luna core, dojoP, ops-mode (unchanged); luna-service deploy (owner).
