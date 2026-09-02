# Phase 3 — execution summary

**Commit:** see `git log` (this phase's commit). No version bump — 0.34.0
ships in phase 4.

## The big discovery: Part A already landed upstream

PLAN.md scoped a luna-service change (token-gated HTTP pass-through for the
card's poll, the missing Part B of plan 014). Mid-phase I found it **already
merged to luna-service main** as plan 077 ("token-gated HTTP pass-through
for playbook delegation card", commit `0e39373`, merged `d3b25b0` on
2026-08-30) — with its own 129-line test file and a *better* design than
mine: a dedicated `_proxy_token_gated_get` that never wakes a machine (an
unauthenticated poll must not keep a tenant awake; a stopped machine can't
be running a delegation → 503).

I had already re-implemented it on the local checkout before finding this;
I reverted my redundant edits and deleted my duplicate test file. The
luna-service working tree is back to exactly the state I found it in
(someone's WIP on branch `fix/n5-render-connector-wiring` — untouched).

**Two flags for the owner:**

1. **Deploy:** the pass-through exists on luna-service `main` but reaches
   hosted tenants only when luna-service is deployed. Until then, hosted
   cards still fall back to the honest offline copy.
2. **Hardening nit in main's version:** `_proxy_request` copies client
   headers and only *sets* `x-luna-user` when a user exists — on the
   token-gated path (user=None) a client-supplied `x-luna-user` header is
   forwarded to the tenant with a valid proxy secret. Not exploitable
   today (the regex pins the path to the card route, which ignores
   identity), but one `headers.pop("x-luna-user", None)` on the None
   branch would close it. I did not commit this: luna-service's AGENTS.md
   forbids branch switching without explicit permission, and the checkout
   sits on someone's WIP branch.

## What shipped (plugin repo)

### Card refresh — `plugin_playbooks/card.py`

Kept: the whole vision grammar (eyebrow → headline → support → phases →
feed → result), dark tokens, and every honest-offline behavior from 0.25.3
(API_BASE from `document.baseURI`, readable 401/403 → offline, five
never-succeeded polls → offline copy, `<` escaping in the boot JSON,
height postMessage capped at 1400). New, on top of phase 2's richer events:

- **Feed open by default** with auto-scroll pinned to the newest entry —
  a reader who scrolled up (or closed the details) stays put. Summary line
  shows a live step count ("Activity · 8 steps").
- **Verdict ticks** per tool row: green ✓ (`ok` true/absent), red ✗
  (`ok === false`), pulsing amber ● while in flight (`ms == null`).
  Thought rows stay quiet italics.
- **Args hint**: first short string value from the scrubbed args, faint,
  next to the mono tool name — the `•••` secret sentinel is skipped, and
  hints reach the DOM only through the escaper.
- **Steps budget counter** in the header while running (`9/40`);
  `card._MAX_STEPS` mirrors `delegation._MAX_TURNS`, pinned by a drift
  test (card.py can't import delegation — the import runs the other way).
- **Terminal states** color a 2px top border (green/red/amber) plus the
  existing headline colors.

### Tests — `tests/test_card_html.py`

Seven new tests: feed open by default, tick wiring, hint escaping +
sentinel skip, budget counter + boot field, `_MAX_STEPS`/`_MAX_TURNS`
drift pin, autoscroll pinning logic, terminal border colors. All 0.25.3
contract tests pass unchanged.

## Verification

- Plugin suite: **352 passed** (345 + 7 new).
- luna-service: `test_proxy_token_gate.py` + wake + streaming proxy tests
  green on the current checkout (37 passed); one pre-existing failure in
  `test_billing_stripe_clawback.py` exists on HEAD before any change and
  was left alone.
- **Real browser against QA Luna** (:8765): seeded a synthetic running
  delegation directly in Postgres (10 events: thoughts, ✓ calls, one ✗
  validate, one in-flight preflight), served a throwaway harness from the
  managed copy's `ui/` dir embedding the card twice — once
  `sandbox="allow-scripts"` exactly as chat renders it, once open for DOM
  asserts. Verified live: headline "Testing — demo-digest", 9/40 counter,
  phases Understand/Change done + Prove current with per-phase counts,
  ticks `✓✓✓✗✓✓✓●` in order, hints shown with the secret sentinel
  skipped, feed pinned to bottom. Then flipped the row to `done` in the
  DB and watched the live card transition: green top border
  (`rgb(58,210,159)`), "Done — demo-digest updated", result block shown,
  counter cleared, all phases done. Screenshots: `card-running.png`,
  `card-done.png` (in this folder). Harness and seeded row deleted after.

## Reassessment of remaining phases

- **Phase 4 (E2E + ship 0.34.0)** — scope unchanged: real delegation
  end-to-end on QA Luna from a chat turn, regression sweep, three version
  stamps, publish to `official`. Two items move INTO phase 4's ship notes:
  remind the owner (a) to deploy luna-service main so hosted cards go
  live, and (b) about the `x-luna-user` hardening nit above.
- The luna-service portion of PLAN.md phase 3 is marked satisfied by
  upstream plan 077 rather than by new code — PLAN.md annotated.
