# 014 — execution summary

Executed 2026-08-29. Shipped as plugin-playbooks 0.25.3.

## What shipped

- `card.py`: the poll now computes `API_BASE` once at boot — try
  `window.parent.__LUNA_BASE` (throws in the sandboxed iframe, caught), fall
  back to regexing `document.baseURI` for `/a/{slug}`, else `''` — and
  prepends it to the card-status fetch. Previously the URL was root-relative,
  so on luna.com.ai (tenants under `/a/{slug}/`) every poll missed the tenant
  and the card sat on "Starting… Connection lost — retrying" forever.
- `card.py`: honest offline state when the poll can never succeed. Readable
  401/403 → offline immediately; or the first 5 polls all fail with no
  success ever (the cloud proxy's 401 carries no CORS header, so the sandbox
  sees only opaque network errors) → offline. Offline = headline "Working —
  live updates can't show here", support "The playbook agent keeps going.
  Ask me how it's going.", polling stops. Transient failures after a
  successful poll keep the old retry behavior.
- `tests/test_card_html.py`: two new tests (base-prefix wiring incl. no bare
  root-relative fetch remaining; offline-state wiring incl. the everPolled
  fallback). Suite: 254 passed.
- Version 0.25.3 in all three stamps (`__init__.py` manifest,
  `luna-plugin.toml`, `pyproject.toml`). 0.25.1/0.25.2 were taken by the
  icon-URL releases, so this plan shipped as .3 not .1.

## How it was verified

- `node --check` on the generated card script; standalone harness confirms
  the base regex yields `/a/vaselin-luna-bug-fixer` for a hosted baseURI and
  `''` for localhost.
- Real-browser QA (localhost:8766, CDP): injected the new card srcdoc into
  the live page exactly as chat-ui embeds it (`sandbox="allow-scripts
  allow-downloads"`), boot data from a real delegation row
  (af200532…, its real card token). The card hit the real route (log shows
  200), rendered the full Done state, posted height messages.
  Screenshot: session scratchpad `p14-qa-card.png`.
- Real-browser hosted (luna.com.ai/a/vaselin-luna-bug-fixer, CDP): same
  injection. Poll goes to the prefixed URL, proxy 401s (no CORS → opaque
  errors), and after 5 failures the card lands on the offline state — no
  endless "Connection lost — retrying". Screenshot: `p14-prod-card.png`.
- Empirical proxy probe: unauthenticated
  `GET luna.com.ai/a/{slug}/api/p/plugin-playbooks/delegations/{id}/card`
  returns a clean 401 (no redirect, no ACAO header) — this drove the
  everPolled fallback design.

## Deviations / notes

- Verified via srcdoc injection into real browsers rather than a fresh dojo
  delegation turn: the change is confined to the card document's JS, and the
  injection reproduces the exact embedding (opaque origin, real route, real
  data) on both QA and hosted. Delegation creation itself was untouched and
  was dojo-verified in plan 013 phase 4.
- Old cards in chat history stay broken — their HTML is baked into past
  messages. Only cards drawn after the upgrade get the fix.
- Part B (luna-service token-gated HTTP pass-through, mirroring
  `_TOKEN_GATED_WS_SUFFIXES`) remains open and needs Roy's authorization;
  until then hosted cards show the offline state instead of live progress.
  When implemented, the proxy must also preserve the tenant response's
  `Access-Control-Allow-Origin: *` header, or the sandboxed fetch still
  can't read the body.
