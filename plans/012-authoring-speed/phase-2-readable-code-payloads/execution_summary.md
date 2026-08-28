# 012 / phase 2 — execution summary

Shipped as **0.20.0**, commit `cd7a09f`, pushed to
huemorgan2/plugin-playbooks main, published to marketplaces.com.ai
(catalog latest_version confirmed 0.20.0), and upgraded on the live
tenant vaselin-scanny-2 (0.19.0 → 0.20.0, hot-loaded: enabled, active,
23 tools, no restart).

## What changed

- `plugin_playbooks/agent_tools.py` — the `playbook_edit` read stage
  (modes==0 in `_edit_impl`) now returns a compact JSON header line
  (stage, editing, versions, ticket, expiry, instructions,
  manifest_note when relevant) followed by plain-text frames:
  `--- manifest ---`, `--- code (candidate vN / live vN) ---`,
  `--- language reference ---`, `--- end ---`. Code carries real
  newlines. Each marker owns its leading newline, so sections
  round-trip byte-exactly. `manifest_text` is captured before
  `session.commit()` (expire_on_commit would make post-commit attribute
  access blow up under async). Write stages unchanged. ToolDef
  description now says the code comes as plain readable text.
- `tests/readstage.py` (new) — `parse_read_stage(text)` returns a dict
  shaped like the old all-JSON payload, so existing assertions
  (`out["ticket"]`, `read["code"] == NEW_CODE`, `out["language_reference"]`)
  survive unchanged.
- 6 test files: read-stage call sites switched from `json.loads` to
  `parse_read_stage` (helpers `_read_stage`/`_ticket` + direct sites).
- 3 new contract tests in `tests/test_code_tools.py`: framed shape +
  header carries no code, snippet-copied-verbatim round trip saves a
  candidate, candidate labeling after a first edit.

## Verification

- Baseline recorded before any change: 186 passed at 0.19.0 (b28368e).
- After: **189 passed** (full suite), zero regressions.
- Production: publish → tenant upgrade → `/api/plugins` shows 0.20.0
  active with all 23 tools and no error (hot-reload convergence).

## Deviations from PHASE.md

- QA-Luna agent-turn probe skipped: the running QA Luna on :8766 belongs
  to a concurrent session (its process, its scratchpad venv, unknown
  owner token) — driving turns on it risks interference. The critical
  property ("old= copied verbatim from the read stage saves without a
  re-read") is covered by `test_read_stage_snippet_roundtrip` at the
  tool layer; production check was hot-load only. First live authoring
  session on the tenant is the real confirmation — watch for it.

## Surprises / learnings

- `playbook_get_definition` already returned raw code — PLAN.md phase 2
  scope was halved (read stage only). PLAN.md not edited; recorded here.
- The tenant had ALREADY moved 0.18.0 → 0.19.0 mid-phase (concurrent
  session ships versions in this repo continuously). Rule for later
  phases: `git pull` + re-read the three version stamps immediately
  before bumping, and expect `from_version` drift at upgrade time.
- SQLAlchemy expire_on_commit: any refactor that moves attribute access
  after `commit()` in these handlers must capture values first.

## Reassessment of remaining phases

- **Phase 3 (payload diet)**: unchanged in substance. One addition — keep
  the `--- language reference ---` frame NAME stable when swapping the
  8KB cheatsheet for the mini-reference, so `parse_read_stage` and any
  agent habit formed on the frame survive. Byte-size assertion for the
  read stage should measure header + manifest + frames WITHOUT code
  (code size is the playbook's own). PHASE.md updated.
- **Phase 4 (real shapes first)**: unchanged. Coordination note stands:
  plans/014 (failed-run digest) is still uncommitted in the working
  tree; re-check `_status` for landed changes before editing it.
