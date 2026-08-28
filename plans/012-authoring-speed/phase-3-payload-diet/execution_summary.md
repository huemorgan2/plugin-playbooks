# 012 / phase 3 — execution summary

Shipped as **0.21.0**, commit `3d07f8a`, pushed to
huemorgan2/plugin-playbooks main, published to marketplaces.com.ai
(catalog latest_version 0.21.0), and upgraded on the live tenant
vaselin-scanny-2 (0.20.0 → 0.21.0, hot-loaded: enabled, active, 23
tools, no restart).

## What changed

- `plugin_playbooks/reference.py` — new `LANGUAGE_MINIREF` (1,120 bytes):
  step kinds, reference shapes, the four rules agents actually forget,
  and a pointer to `playbook_language_reference`. `LANGUAGE_CHEATSHEET`
  gained the LOOP CONFIG detail moved out of the skill (grew 4,968 →
  5,845 bytes; it must stay complete — it is the recall surface on
  failed validate/compile and behind the reference tool). Module
  docstring updated to describe the new split.
- `plugin_playbooks/agent_tools.py` — the read stage's
  `--- language reference ---` frame now carries `LANGUAGE_MINIREF`
  instead of the full cheatsheet. Frame NAME unchanged, so
  `tests/readstage.py` and any agent habit formed on the frames survive.
  All other cheatsheet attach points (compile error, failed validate,
  `playbook_language_reference`) keep the full sheet.
- `plugin_playbooks/__init__.py` — `_AUTHORING_SKILL_BODY` dieted
  22,695 → 12,258 bytes. Removed wholesale (now cheatsheet-only): THE
  LANGUAGE exact rules, CODE STEPS, FUNCTIONS, loop-config kwargs,
  state-ops list. Kept: THE LOOP workflow, decomposition steering
  (THE POINT / AGENT DECIDES / validator codes / rules of thumb), both
  compile-verified examples (subscription-scan, site-crawl — the
  ≥2-blocks compile test requires them), CONTEXT ECONOMY, REFERENCE
  SHAPES, edit/candidate/spec/preflight flows. A stale phase-2 leftover
  fixed on the way: the MANIFEST section described the read stage as
  all-JSON; it now describes the framed format.
- `tests/test_authoring_ergonomics.py` — read-stage reference assertion
  now expects `LANGUAGE_MINIREF`; new `test_payload_diet_budgets` pins
  skill body ≤12KB, miniref ≤2KB, and read-stage overhead (payload
  minus code+manifest) ≤6KB.

## Verification

- Full suite: **190 passed** (189 baseline + 1 new), zero regressions;
  both skill example blocks still compile and round-trip.
- Measured: skill body 12,258B (budget 12,288), miniref 1,120B,
  read-stage overhead well under 6KB.
- Production: publish → catalog 0.21.0 → tenant upgrade → `/api/plugins`
  shows 0.21.0 active, 23 tools, no restart.

## Deviations from PHASE.md

- None in scope. QA-Luna turn probes skipped as pre-declared (other
  session owns :8766).

## Surprises / learnings

- The luna.com.ai Chrome tab had been navigated away by the concurrent
  session — the CDP recipe's "reuse the luna.com.ai tab" step must be
  prepared to PUT /json/new + Page.navigate first (as phase 2 learned).
- `proxy-login` returns `{access_token, token_type}`, not `{token}` —
  the upgrade endpoint 401s ("Invalid token" with `Bearer undefined`)
  while cookie-backed GETs keep working, which masks the bug. Use
  `access_token`.
- Dieting prose to a byte budget converges much faster measuring
  per-section sizes (`re.split(r'(?m)^### ', body)`) than trimming
  blind.

## Reassessment of remaining phases

- **Phase 4 (real shapes first)**: unchanged in substance. The skill
  body now has ~30 bytes of headroom under its pinned 12KB budget —
  phase 4's planned skill prose line about failed runs must fit inside
  it (trim elsewhere if needed; do not raise the budget). Coordination
  note stands: re-check `_status`/spec_from_run for plans/014 landed
  changes before editing. PHASE.md updated.
