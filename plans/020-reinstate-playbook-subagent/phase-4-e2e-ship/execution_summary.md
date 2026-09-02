# Phase 4 — execution summary (plan 020 complete)

**Version:** 0.34.0, published to `official` (catalog `latest_version`
verified). **Release commit:** `6c3d416`, pushed to origin/main.
Phase commits: `dd10ace` (1), `1e28bcd` (2), `5bb554e` (3).

## What shipped

- Three version stamps bumped to 0.34.0 (`__init__.py`,
  `luna-plugin.toml`, `pyproject.toml`; `test_version_stamps_agree` pins
  them together).
- Packaged with `scripts/package_plugin.py` (27 files, single top-level
  `plugin_playbooks/` dir) and published via `publish_plugin.sh` to
  `official`.
- QA Luna's managed copy swapped to the released code and restarted:
  clean load (zero tracebacks; the only manifest drift warning is
  pre-existing and belongs to plugin-web-access), UI 200, card route
  answers its single no-oracle 404.

## Regression sweep on the release commit

- pytest: **352 passed** (includes `test_ops_exceptions.py` — 6 passed,
  explicitly, per the brief's hard constraint; `ops_provider.py` was
  never resurrected).
- UI: vitest **138 passed** (18 files); `tsc -b && vite build` clean
  (the pre-existing >500 kB chunk warning only).

## E2E coverage — what this session proved, and the one owner step

Proven for real on QA Luna across phases 1–4: the `playbook_delegations`
table (12 columns), the capability-token card route (single 404 for
unknown id AND bad token), the authed bench API (401 unauthenticated;
list/detail shapes unit-tested), and the card itself — rendered in a
`sandbox="allow-scripts"` srcdoc iframe exactly as chat does, polling the
real route, showing verdict ticks / args hints / the 9-of-40 counter /
phase progression, then transitioning live to the green Done state with
the final report. Screenshots in `../phase-3-card/`.

The one path no headless session can drive is a real chat turn (the main
agent calling `playbook_agent` with a live LLM). **Owner script, ~5 min,
on any agent running 0.34.0:**

1. Fresh conversation → say: *"Use the playbook agent to build me a tiny
   playbook that fetches https://example.com and reports its title."*
   The agent should load the `playbook-delegation` skill and call
   `playbook_agent` (auto-approved) — a progress card appears as its own
   chat row and starts ticking.
2. Watch the card: feed open, ✓/✗ ticks, steps counter. Somewhere near
   the end the card shows the amber "Waiting for your approval — make the
   change live" banner and the publish approval card arrives.
3. Approve it. The card flips green ("Done — … updated") with the
   delegate's final report; the playbook is live. Ask
   `playbook_agent_status` anything if curious.

## Reminders for the owner (carried from phase 3)

1. **Upgrade the plugin on your agents** (marketplace upgrade) —
   publishing does not auto-upgrade installed agents.
2. **Deploy luna-service main** — the card's token-gated pass-through
   (upstream plan 077) reaches hosted tenants only after a deploy; until
   then hosted cards show the honest offline copy. Cards on self-hosted /
   QA agents work now.
3. Optional hardening nit in luna-service `_proxy_request` (client-sent
   `x-luna-user` not stripped on the token-gated path — details in
   `../phase-3-card/execution_summary.md`).

## Reassessment of remaining phases

None remain — plan 020 is complete. The subagent is back, modernized
(plan-gate, building-state, 11-section just-in-time prompt), its work is
bench-readable over the authed API, and the chat card — the part that
"never really worked" — is browser-verified working and worth watching.
