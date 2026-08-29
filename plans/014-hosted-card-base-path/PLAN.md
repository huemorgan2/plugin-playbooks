# 014 — Hosted delegation card: base path + honest offline state

Status: EXECUTED 2026-08-29 — Part A shipped as 0.25.3; Part B (luna-service) pending authorization

## Problem

On luna.com.ai the delegation progress card (plans/013 phase 2) is stuck on
"Starting… Connection lost — retrying" forever, even while the delegate runs
fine. Seen live on vaselin-luna-bug-fixer (three cards, 2026-08-28).

Two independent causes, confirmed by reading the chat UI and the cloud proxy:

1. **Wrong URL.** `card.py` polls a root-relative URL
   (`fetch('/api/p/plugin-playbooks/delegations/<id>/card?token=…')`).
   Hosted tenants live under a path prefix — `luna.com.ai/a/{slug}/…` — so the
   poll hits `luna.com.ai/api/…`, which doesn't exist. QA (localhost:8766,
   no prefix) can't catch this: srcdoc iframes inherit the parent page's base
   URL, and with no prefix the relative URL happens to be right.

2. **Proxy auth.** Chat embeds render in `sandbox="allow-scripts
   allow-downloads"` iframes (luna/ui/src/views/ChatPanel.tsx) — opaque
   origin, no session cookie, no `window.parent` access. The cloud proxy's
   `proxy_to_luna` (luna-service cloud/api/proxy.py) requires a session via
   `_resolve_agent` → even a correctly-prefixed poll gets 401. The tenant
   route itself already does capability-token auth (`secrets.compare_digest`,
   404 no-oracle) and sends `Access-Control-Allow-Origin: *`, so it is safe to
   forward unauthenticated — the proxy just doesn't.

Precedent for cause 2: the WebSocket proxy already has
`_TOKEN_GATED_WS_SUFFIXES` ("api/p/luna-macrunner/ws") — forwarded by slug,
tenant does the auth (luna-service plan 062). The HTTP proxy has no
equivalent.

## Fix

### Part A — this plugin (this plan executes it)

1. `card.py`: compute an API base once at boot and prepend it to the poll URL:
   - try `window.parent.__LUNA_BASE` (the proxy injects it into the SPA HTML)
     inside try/catch — throws in the sandboxed iframe, harmless;
   - fall back to regexing `document.baseURI` (inherited from the parent
     page) for `^[a-z]+://[^/]+(/a/[^/]+)` → e.g. `/a/vaselin-luna-bug-fixer`;
   - else `''` (self-hosted / QA — unchanged behavior).
2. `card.py`: stop lying when the poll is auth-blocked. Two triggers, because
   the proxy's 401 has no `Access-Control-Allow-Origin` header and therefore
   surfaces in the opaque-origin sandbox as a network error with an
   unreadable status: (a) a readable 401/403 → offline immediately; (b) the
   first 5 polls all fail with no successful poll ever → offline. Offline
   state: headline "Working — live updates can't show here", support "The
   playbook agent keeps going. Ask me how it's going.", polling stops.
   Transient errors after a successful poll keep the existing
   "Connection lost — retrying" behavior.
3. Regression tests in `tests/test_card_html.py`: card HTML contains the
   base-resolution code, the fetch uses the computed base, no bare
   root-relative `fetch('/api` remains, 401 handling present.
4. Bump 0.25.3 (three stamps: `__init__.py` manifest, `luna-plugin.toml`,
   `pyproject.toml`), full pytest, QA-verify a live delegation card on
   localhost:8766 (prefix `''` path must still work), commit, push, publish,
   re-upgrade hosted agents.

### Part B — luna-service (NOT executed here; needs Roy's authorization)

`cloud/api/proxy.py`: add a token-gated HTTP pass-through mirroring the WS
precedent — GET requests whose path matches
`api/p/plugin-playbooks/delegations/{id}/card` are resolved by slug and
forwarded without a session; the tenant route's capability token is the auth.
Until Part B ships, hosted cards show the Part A offline state instead of
live progress; QA/self-hosted cards work fully. Old cards in history stay
broken either way (their HTML is baked into past messages).

## Verification

- pytest suite green (253+ tests).
- QA 8766 dojo: fresh delegation → card polls with empty prefix, reaches
  done state, zero console errors.
- Hosted (after upgrade): fresh delegation on a real tenant → card shows the
  honest offline state (not "Connection lost — retrying"); after Part B, live
  progress.
