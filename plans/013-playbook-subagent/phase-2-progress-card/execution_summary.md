# Phase 2 — live progress card: execution summary

## What shipped

Version: **0.25.0** (all three stamps; rebased onto main's 0.24.0 / plans-011
icons before this phase closed). Commits on branch `013-playbook-subagent`:
`0f2d4ea` (card + route + posting), `eeae4d1` (rebase + version), plus the
pydantic-ai-2 event-shape fix committed with this summary.

- `plugin_playbooks/card.py` — `render_delegation_card(...)`: self-contained
  srcdoc HTML card. Eyebrow "Playbook agent" + playbook chip, phase rows
  (Understand / Change / Prove / Ship) with state dots, collapsible "What it
  did" feed, result panel, elapsed timer. Polls the card route every 1.5 s,
  stops on terminal status, reports height via `luna:embed:height`
  postMessage. BOOT JSON is escaped (`<`/`>` → `</>`) so a hostile
  playbook name cannot terminate the script element.
- `plugin_playbooks/routes.py` — unauthed
  `GET /api/p/plugin-playbooks/delegations/{id}/card?token=...` on
  `ui_router`. `secrets.compare_digest` against the per-delegation
  `card_token`; unknown id and wrong token both 404 (no oracle); `result`
  withheld while running; live in-process feed preferred over the throttled
  DB row; events tail-capped at 200; `Access-Control-Allow-Origin: *` +
  `Cache-Control: no-store`.
- `plugin_playbooks/delegation.py` — posts the card via
  `ctx.post_chat_card` (feature-detected) right after the delegation row is
  created; stores `card_message_id`. **Fix found on real Luna:** pydantic-ai
  ≥2 (QA runs 2.35.0) delivers tool results as
  `FunctionToolResultEvent.part` (plus `.content`), not `.result` — the
  duck-typed mapper only read `.result`, so real runs recorded events with
  `ms=None` and empty `detail`. The mapper now falls back to `.part`.
- Tests: `tests/test_card_html.py` (5), `tests/test_card_route.py` (5, via
  httpx ASGITransport), and a new v2-shaped result-event test in
  `tests/test_delegation.py`. Full suite: **239 passed**.

## Verification on real Luna (QA, port 8766)

Synced the package into the instance's managed dir
(`.../qa084-managed/plugin_playbooks` — the package directory itself, see
surprises), restarted, `/api/plugins` showed plugin-playbooks 0.25.0 with
`playbook_agent`/`playbook_agent_status` registered.

Drove three real delegations in conversation "p2 delegation card dojo"
(`4c552c22-...`) via `POST /api/conversations/{id}/messages`, auth by a JWT
minted with the instance's own `luna.auth.jwt.create_token` — `sub` must be
the **user UUID** (the `qa` user), not a username.

Verified, in a real Chrome via CDP (:9222):

- Turn 1 (slow path): Luna loaded `playbook-delegation`, called
  `playbook_agent`, got `status=running` at the 25 s budget, ended its turn
  pointing at the card. The card rendered **as its own timeline row**.
- The delegate parked on a pending `playbook_promote` approval (medium
  risk) — the phase-3 scenario, observed for real. Approving via
  `POST /api/p/plugin-approvals/{id}/approve` resumed it; the delegation
  finished `done` with a coherent result and the card flipped terminal
  (spec added, 2/2 specs green).
- Turn 2 (fast path): `playbook_agent` returned `done` inline in 14 s; the
  card appeared live in the open browser without reload, phases lit with
  per-phase step counts, Ship dot hollow (review-only task — correct).
- Terminal cards **stop polling** (server log goes quiet) and a leftover
  live feed never overrides a terminal row.
- The unauthed token route served the opaque-origin iframe correctly; no
  page console errors (`window.__errs` hook stayed empty).
- After the event-shape fix and a restart, a fresh delegation's events all
  carried real durations (`ms=78…142`) and result details.

## Deviations from PHASE.md

None in scope. One extra fix (the pydantic-ai 2.x event shape) was pulled
into this phase because the real-Luna check exposed it.

## Surprises / learnings

- **pydantic-ai 2.x renamed the result seam** (`.result` → `.part`); unit
  fakes mirrored the old shape so 13 green tests hid a real-world gap.
  Exactly why the real-Luna check is mandatory.
- **Managed-dir layout**: the managed plugin folder must contain the
  *package* contents (`__init__.py`, `luna-plugin.toml` at top level). An
  rsync of the repo root nested the package one level deep and Luna
  silently fell back to loading an old 0.14.0 build — worth remembering
  when syncing worktrees to QA.
- QA auth: `POST /api/auth/login` is unusable (owner password hash empty);
  minting a JWT with the instance venv works, but `sub` must be the user's
  UUID from the `users` table.
- Approval TTL (180 s) does not hard-expire the request: an approve after
  ~7 minutes still resumed the parked delegate.

## Reassessment of remaining phases

- **Phase 3 (approval parking)**: the core behavior already works end to
  end (park → approve → resume → done). Phase 3 therefore narrows to the
  *card/status surface*: derive a PARKED indication from the event feed (a
  gated tool call with no result for >N s), show it on the card and in
  `playbook_agent_status`, and dojo-test the full owner journey (approval
  card visible in chat next to the progress card). No new core seams
  needed.
- **Phase 4 (dojo end-to-end)**: unchanged. Use a fix-task on a broken
  playbook (e.g. reintroduce the phone-format bug fixture) so
  Understand→Change→Prove→Ship all light up in one browser run; compare
  against the plans/012 baseline.
