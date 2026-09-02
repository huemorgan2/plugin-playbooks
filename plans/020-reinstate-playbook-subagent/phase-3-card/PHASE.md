# Phase 3 — the card actually works, and looks the part

The chat card was the part the owner remembers never working. Two causes:

1. **Hosted tenants:** the card's poll goes to
   `/a/{slug}/api/p/plugin-playbooks/delegations/{id}/card?token=…` on
   luna-service, whose HTTP proxy (`cloud/api/proxy.py::proxy_to_luna`)
   unconditionally requires the owner's session cookie. The sandboxed srcdoc
   iframe has an opaque origin and sends no cookies → 401 with no CORS
   header → looks like a network failure → offline copy. Part B of plan 014
   (token-gated HTTP pass-through) never landed; only the WS variant
   (`_TOKEN_GATED_WS_SUFFIXES`) did.
2. **Visuals:** the 0.25.3 card is functional but austere — a collapsed
   "What it did" feed, label-only rows. The owner wants to *watch* the
   delegate work.

## Scope

### A. luna-service: token-gated HTTP pass-through (`cloud/api/proxy.py`)

- `_TOKEN_GATED_HTTP_GET_RE`: compiled regex, exactly
  `api/p/plugin-playbooks/delegations/<uuid-ish>/card` (matched against
  `path.rstrip("/")`). GET only.
- In `proxy_to_luna`: when it matches, resolve the agent **by slug only**
  (404 if unknown) and skip session/membership entirely — the tenant plugin
  is the authority (capability token, compare_digest, single-404 no-oracle).
  Wake logic unchanged. All other paths keep the existing session auth.
- `_proxy_request` gains `user: User | None`; `x-luna-user` header set only
  when a user exists. Response headers stream through untouched, so the
  plugin's `access-control-allow-origin: *` + `no-store` survive.
- Tests: `cloud/tests/test_proxy_card_passthrough.py` — anon GET card path
  proxied; anon GET other paths still 401; POST to card path 401; unknown
  slug 404; regex doesn't over-match (`…/card/extra`, other plugins).
- Commit + push luna-service. **Deploy is manual** — flag for the owner.

### B. Card visual refresh (`plugin_playbooks/card.py`)

Keep the vision grammar (eyebrow → headline → support → phases → feed →
result), dark tokens, and every honest-offline behavior from 0.25.3
(API_BASE from baseURI, readable 401/403 → offline, 5 straight
never-succeeded polls → offline copy, height postMessage capped 1400, `<`
escaping in boot JSON). On top:

- **Live feed, open by default** with auto-scroll pinned to the newest
  entry (unless the user scrolled up), so the execution is visible without
  a click. User toggle respected once touched.
- **Per-call verdict ticks** from phase 2's `ok` field: ✓ (green) / ✗ (red)
  per tool row; thoughts keep the italic quiet style.
- **Args hint**: one faint inline hint per tool row from the scrubbed
  `args` (first short string value, e.g. the playbook name) — escaped.
- **Terminal states more distinct**: done/failed/needs_owner keep their
  headline colors and get a matching thin top border on the card.
- Steps counter in the eyebrow region while running (`n/40`).

Tests: extend `tests/test_card_html.py` for ticks/ok rendering, feed open
by default, args-hint escaping of hostile input; existing tests keep
passing.

### C. Verification

- Plugin suite green; luna-service suite (at minimum the proxy tests + a
  full `cloud/tests` run if runtime permits).
- Real-env: rsync managed copy, restart QA Luna, insert a synthetic
  delegation row (running → later finished) directly in Postgres, drop a
  throwaway harness page into the **managed copy's** `ui/` dir (same
  origin :8765, never committed) embedding the rendered card in an iframe,
  drive it with a real browser (CDP): assert the card polls the real route,
  renders phases/ticks/args, and shows the terminal state; screenshot for
  the record. Remove the harness afterwards.

## Out of scope

Old cards already in chat history (unfixable by design — they polled with
tokens of dead delegations); luna-service production deploy (owner);
0.34.0 version bump (phase 4).

## Verification criteria

- luna-service: new tests green, no regression in existing proxy tests.
- plugin: full suite green (expect ~350).
- Browser-verified live render on QA Luna, including terminal state.
