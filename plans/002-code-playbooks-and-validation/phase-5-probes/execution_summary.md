# Phase 5 — Preflight probes: execution summary

Shipped as **plugin-playbooks 0.12.0** + luna core **0.34.020** (local commit
b00b0b9, plans/038 — NOT pushed; the plugin consumes ProbeDef duck-typed and
runs unchanged on older cores).

## What was built

- **luna SDK (plans/038)**: `ProbeDef` (`kind: auth|resource_read`, optional
  async `handler`, optional `args` that call the tool's own handler) + optional
  `ToolDef.probe`. Exported from `luna_sdk`. Duck-typed consumption — no core
  version dependency for the plugin.
- **`probes.py`**: `collect_tools` / `collect_subtasks` (full IR walk:
  then/else/body/branches, agent allowlists; subtask targets followed one level
  via their LIVE definitions), `probe_tool` (never raises; ok / unprobeable /
  failed with failure classes tool_missing, blocked, credential_dead,
  resource_gone, permission, rate_limited, unknown), `run_preflight` (upserts
  `playbook_probe_results` cache rows, drops stale ones), `reprobe_enabled`
  (daily sweep body — alerts only on NEW failures), `preflight_note`.
- **Promote gate 4 "probes"** in both the tool path and REST promote: only
  `failed` blocks; `unprobeable` passes with a note. Refusal names the broken
  tools + failure classes; cache rows committed even on refusal.
- **`playbook_preflight` tool** (skill-gated, in AUTHORING_TOOLS): targets
  auto/candidate/live/N via `_spec_target`; BROKEN steering on failures, honest
  "nothing verified" note when no tool declares a probe.
- **REST**: `GET /playbooks/{name}/probes` (cache, for phase-6 trust badges),
  `POST /playbooks/{name}/preflight`.
- **Daily re-probe loop**: `app.router.on_startup` task (30s warmup, then every
  24h); new failures notify the agent via `ctx.send_muted_message(channel="moment")`.
- **Dogfood**: `playbook_list` declares a `resource_read` probe (plugin DB
  `select 1`) when the SDK has ProbeDef.
- `luna-plugin.toml` db_tables/requires corrected to the real 8 tables
  (edit_tickets and specs had drifted too); manifest tests updated.

## Verification

- **Unit**: 147 passed (`tests/test_probes.py` — 20 new: collector IR walks,
  full probe_tool matrix incl. args-mode and handler-wins, cache
  upsert/stale-drop, subtask follow, both promote-gate outcomes, tool
  steering/targeting, gating, reprobe transition detection, note wording).
- **Live QA (:8766, real agent turns)**:
  - REST POST /preflight → send_chat_message unprobeable; GET /probes + DB rows
    confirmed.
  - Daily sweep fired 30s after boot and probed all enabled playbooks
    unprompted (`playbook_probe_results` rows for the three phase2 playbooks).
  - Turn J: agent chose `playbook_preflight`, reported 0 ok / 1 unprobeable /
    0 failed faithfully, no over-claiming.
  - Turn K: description-only edit → promote showed FOUR gates
    (`static_validation`, `specs 1/1`, `manifest_drift`, `probes 1 unprobeable`)
    and promoted v8.

## Surprises / lessons

- The two stale July approval cards (set_mission da89fbc6, playbook_set_autonomy
  50eec94e) got approved by this phase's auto-approve monitor. No side effects —
  their turns were long dead, so the continuations never ran — and the phase-7
  "sweep stale cards" item is now moot. Lesson: filter auto-approve loops by
  `payload.tool`.
- `luna-plugin.toml` db_tables had silently drifted two tables behind the
  models since 0.10/0.11; the manifest test pinned the stale list rather than
  the models. Kept the test but synced both to reality.
- probe results for a failing gate must be committed BEFORE returning the
  refusal (same pattern as the specs gate) or the trust surface shows nothing.

## Reassessment of remaining phases

- **Phase 6 (UI trust surface)**: unblocked and cheaper than planned — GET
  /specs and GET /probes both exist and return UI-shaped JSON with cached
  timestamps. Badge logic: specs last_result + probe status per tool; "failed"
  probe → red banner on the playbook card.
- **Phase 7 (cleanup)**: drop the stale-approvals sweep (done, see above).
  Remaining: normalize step-output tool results to dicts (JSON strings pass
  through the stub seam as-is), prune QA leftovers (qa-code-hello now at
  8|8|NULL, autonomy agent_may_trigger; phase0/phase2 test playbooks).
