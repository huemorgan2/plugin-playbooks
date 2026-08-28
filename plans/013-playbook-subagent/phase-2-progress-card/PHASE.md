# Phase 2 — progress card

Scope: the owner-facing live surface. A self-contained srcdoc card posted
into the chat when a delegation starts (`ctx.post_chat_card`, luna 056),
polling an unauthed capability-token route for status while running.
No approval-parking UI yet (phase 3) beyond honest "running" states.

## Deliverables

- `plugin_playbooks/card.py` (new): `render_delegation_card(delegation_id,
  token, playbook, version) -> str` — ONE self-contained HTML document
  (inline CSS/JS, no external fetches except the status poll). Per
  vision/ux_guidelines.md (read 2026-08-28, mandatory):
  - Eyebrow `PLAYBOOK AGENT` + playbook-name chip (chip omitted for
    from-scratch jobs until the delegate names one — future nicety, not
    in scope).
  - Bottom-line headline from status + latest phase; support line = the
    latest event in plain words; elapsed timer top-right.
  - Four phase rows — Understand, Change, Prove, Ship — with 7px status
    dots: hollow pending, amber current, green done, red failed. A phase
    is "done" when a later phase has tool events; "current" = phase of
    the newest tool event.
  - Collapsed-by-default detail feed: one line per tool call (label +
    duration), delegate thoughts in dim type. `<details>`-based.
  - Dark tokens from the guidelines; NO gradient (not a hero); Inter
    stack with system fallback; `prefers-reduced-motion` honored.
  - Height auto-report via `postMessage({type:"luna:embed:height"})`
    (the chat-ui contract, same as plugin-inline-code-run's card).
  - Poll `GET <origin>/api/p/plugin-playbooks/delegations/{id}/card?token=…`
    every 1.5s while running (srcdoc iframes inherit the parent base URL,
    so a relative URL reaches the Luna host); back off to stopped when
    the status is terminal. Fetch failures render a dim "connection lost,
    retrying" support line — never a broken card.
- `plugin_playbooks/routes.py`: `GET /delegations/{delegation_id}/card`
  on the UNAUTHED `ui_router` — `secrets.compare_digest` against the
  row's `card_token` (404 on mismatch — no token-validity oracle), reads
  the in-process `_LIVE_FEEDS` first (fresher than the 1/s DB flush),
  falls back to the row. Returns `{status, playbook, steps_used,
  started_at, finished_at, result, events}` (events tail-capped at 200)
  with `Access-Control-Allow-Origin: *` (the srcdoc iframe posts from an
  opaque origin; the route is already capability-scoped to one
  delegation by the token, so CORS-open is correct).
- `plugin_playbooks/delegation.py`: after the row is created,
  `getattr(ctx, "post_chat_card", None)` → post the rendered card into
  the origin conversation; store the returned message id in
  `card_message_id`. No card on older cores — the tools still work.

## Verification

- Unit tests (`tests/test_card_route.py`): route returns 404 for wrong /
  missing token and unknown id; right token returns the running shape;
  live-feed freshness (feed events visible before DB flush); CORS header
  present; events tail-capped; terminal shape carries result.
- Card HTML tests (`tests/test_card_html.py`): renders with id+token
  baked in exactly once; no external resource URLs (`http` only in the
  poll path); all four phase labels present; escapes a hostile playbook
  name (`<script>` in name).
- Full pytest suite green.
- Real-Luna check: load 0.24.x on QA Luna (port 8766, managed_plugins
  sync), drive a real delegation from a dojo conversation, open the chat
  in a real browser (CDP), confirm: card appears as its own row, phases
  advance live, terminal state lands, no console errors. This closes the
  deferred phase-1 real-Luna verification too.

## Ship

Stays version 0.24.0 (phase 1 has not shipped anywhere yet — one version
covers plan 013 through this phase; the plan ships as a whole at close).
Commits on the worktree branch.
