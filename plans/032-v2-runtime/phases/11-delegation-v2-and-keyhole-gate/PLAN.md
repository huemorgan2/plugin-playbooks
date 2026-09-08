# 032 — Phase 11: Delegation v2 prompt, candidate-conflict guard, author stamping, end-to-end script, keyhole gate
Status: blocked — keyhole gate stop
Master: §2 Sub-agents (v2 prompt variant, delegate toolset unchanged, candidate-conflict guard, author identity on every version row and in the versions UI); §2 Lifecycle ("propose = candidate, always" closes the delegation side door); §2 Prompt surface (`validated: true`, no validate after a green write); §2 Result provenance and `playbook_overview` (candidate `author`); §3 P4 (conflict guard, scripted end-to-end, keyhole bench gate); §3 Success criteria (stop rule); master phase M4
Repo / branch: plugin-playbooks v2-runtime (HEAD 8c31a60 at writing — the branch tip is now 5306f7f, the plan-writing commit on top of it with no code change, so every citation still holds; phases 00-10 land first — every delegation.py / agent_tools.py / routes.py line below is at 8c31a60 and shifts after phase 00's deletions and phases 04/09; cite the symbol when in doubt)
Depends on: plugin/04 (propose = candidate, `Playbook.format`, edit-error payload with `ticket_still_valid`, `validated: true`), plugin/08 (lifecycle integration; the live_version-writer invariant test), dojop/02 (the keyhole gate run — it runs against THIS phase's build and its verdict closes this phase; see Risks 1); through them plugin/05 (THE LOOP wording and `V2_SKILL_BODY`, which the v2 prompt mirrors) and plugin/09 (`playbook_overview` reads the candidate `author`). 2026-09-08 (phase 08 landed, stamp **0.52.0**, `phases/08-*/execution_summary.md`): `playbook_run` on an `agent_must_confirm` playbook returns `{run_id, playbook, status: "parked", approval_id: <str UUID>, message: "run <id> waiting on owner card #<uuid> — tell the user; nothing to poll"}` (no `needs_approval`, never names `playbook_set_autonomy`); `manual_only` → `{status: "refused", playbook, reason}`; `playbook_set_autonomy` results carry a permanence `note`; `playbook_status` on a parked run says `parked on approval #<uuid> — nothing to poll. The owner has the card; the run resumes by itself when they decide (due <iso>).`; the publish gate on a parked candidate run refuses with `candidate run <id> of version <N> is parked on owner card #<aid> — it is neither green nor failed yet.` + hint `Do NOT start another candidate run — it would raise a second card.`; cards of `is_test` runs carry eyebrow `test run of candidate v<N>` and a `[test run of candidate v<N>] ` summary prefix; the parity suite `tests/test_v2_parity.py` (18 tests) pins the delegation card token flow for a python playbook (`test_delegation_card_token_flow_for_python_playbook`) and `test_v2_live_version_invariant.py` whitelists the writers by symbol (a delegation-side live write is red).
2026-09-08 (phase 09 landed, stamp **0.53.0**, 26 tools; `phases/09-provenance-and-overview/execution_summary.md`): the envelope the end-to-end script asserts is the FIRST five keys, in this order, of every run-shaped result — `kind` ∈ {`real_run`, `candidate_test_run`, `dry_run`}, `side_effects` (bool; false only for `dry_run`), `version` (int), `version_role` ∈ {`live`, `candidate`, `historical`}, `run_id` (str UUID; `null` for `dry_run`) — on `playbook_run`, `playbook_run_candidate`, `playbook_dry_run`, `playbook_status` and each entry of `playbook_runs.runs` (the list itself carries `playbook`, `runs`, `next` — no envelope); `playbook_run_candidate` → `kind: candidate_test_run, version_role: candidate, version: <candidate>`; `playbook_dry_run` → `status: simulated` (v1's `done` is mapped at the tool boundary; `simulated_nothing_exercised` and `failed` pass through), `run_id: null`; refusals (`{status: refused, ...}`, `{error: ...}`), the parked result's keys after the envelope (`playbook, status: parked, approval_id, message, next`) and `playbook_overview` carry no envelope. `playbook_propose` now returns `next` = `Candidate v<N> saved and validated. Test it with playbook_run_candidate (playbook_dry_run simulates it first), then playbook_publish(name='<name>') to make it live. playbook_overview(name='<name>') is the truth surface — read it before describing this playbook's state.` — it does not name `playbook_validate` (pinned by `tests/test_v2_overview.py::test_next_hints_point_to_overview`); `playbook_edit`'s candidate_saved `next` ends with the same overview sentence; `playbook_publish`, `playbook_set_autonomy`, `playbook_preflight`, terminal `playbook_status`, `playbook_runs`, `playbook_run`, `playbook_run_candidate` all carry `next` = the overview pointer. `playbook_overview(name)` returns `{playbook, format, playbook_run_executes {version, reason}, candidate {version, author, saved_at, last_test_run {run_id, status, at}|null}|null, runs_of_live_since_publish, parked_runs [{run_id, parked_on}], pending_approvals [{approval_id, kind, run_id}], autonomy, versions [{version, created_at, author, message, promoted_from, live, candidate}] (newest first, cap 10), more {parked_runs, pending_approvals, versions}, next}` — `candidate.author` is the version row's `author` column (this phase's delegate stamping lands there). The overview is NOT skill-gated and NOT in any SkillDef `tools` list (luna's SkillDef contract: listed tools must be `skill_gated=True`); the provenance rule sentence is in both skill bodies (`V2_SKILL_BODY` 5770 bytes of 6144).
2026-09-08 (phase 10 landed, stamp **0.54.0** ×3 — this phase takes **0.55.0**; code `da264d0`; `phases/10-canvas/execution_summary.md`): the canvas step of the end-to-end script can be asserted server-side, no browser — `GET /api/p/plugin-playbooks/playbooks/{name}/graph?version=<n>` → `{name, version, format: "python", triggers, node_ids, root}` with `node_ids` = `["trigger-0", …]` + one `step-<call_site_id>` per call site the checker summary lists (`{f"step-{c['id']}" for c in definition["call_sites"]}` ⊆ `set(node_ids)`), containers `if-/for-/while-/try-<first inner site id>`, `gather-<first argument id>`, `compute-*` nodes; a pblang version → 409 `{"error": "version <n> of '<name>' is pblang — the canvas builds pblang graphs client-side"}`; `GET …/playbooks/runs/{run_id}` of a journaled (python) run gains `trace` (rows `{seq, node: "step-<id>", call_site_id, occurrence, kind, journal_status, status, error, dry, started_at, ended_at, ms, args, result}`; a failed run's last row is the failing site), `failed_line`, `error`, `error_type`, `traceback` — a pblang run's payload has none of these keys (byte-identical to phase 09). `GET …/playbooks/{name}/versions/{n}` now carries `format`. UI bundle pair `index-BmqqWkXy.js` / `index-N9IomYre.css`; the interim `V2View` is gone (python versions render `V2Canvas` + `V2NodePanel`). Phase 10's Step 10 (side-load, owner visual check, v1 regression-gate run) is DEFERRED to M6 by owner decision — the M4 gate line "canvas check" reads `deferred to M6`, not `done`.
Unblocks: plugin/12 (migration + final measurement), dojop/02's gate run (needs this build), luna-fixer M4 gate

## Goal
After this phase a delegated authoring job on a python playbook gets a prompt that teaches the v2 loop (write → validated on save → dry run → real candidate run → publish) in the same 11-section shape as today, with the same toolset. Every version row says who wrote it — `agent`, `delegation:<id>` or `owner` — and the versions UI shows it. Saving over another author's unpublished candidate is refused with a message naming that author and version; the same author still iterates on its own candidate as before. A scripted end-to-end test drives the real handlers through propose → dry run → real candidate run → approval-gated publish with no validate call after the green write. dojop/02's keyhole gate (7 live tasks, one run, ≥ 2 trials, pass^k ≥ 1.00, zero honesty / `must_not_call` violations, wrong-claim counts code-graded) runs against this build and its verdict is recorded here; red stops the plan.

## Scope — changes
Writer identity (`plugin_playbooks/delegation.py`):
- New module-level `_delegation_id: ContextVar[uuid.UUID | None]` and `def writer_identity() -> str`:
  `f"delegation:{id}"` when set, else `"agent"`.
- `_drive_delegation` (:574-638) sets it with a token before `ctx.agent.run_turn(...)` (:593-603) and
  resets it in the existing `finally` (:637-638).
- The handlers the delegate calls run inside that `run_turn` call chain (luna `agent/runtime.py:2309`
  awaits the handler; tasks spawned there inherit the context), so the ContextVar reaches
  `_edit_impl` / `_propose` / `_manifest_set` without touching the toolset (`delegate_toolset`
  :170-193 unchanged). Owner paths never set it: routes.py stays `author="owner"` (:801, :1540).
- No fallback by conversation id: the delegate runs in the calling conversation
  (`run_turn(conversation_id=...)` :598; `PlaybookDelegation.conversation_id`, models.py:286) and the
  chat agent may write there concurrently. If the context does not propagate on the real core
  (Step 10) the phase stops and re-plans (Risks 2).
- Toolset helper for python targets: `_referenced_tools` (delegation.py:146-167) runs
  `PlaybookDef.model_validate(definition)` and walks `steps` — `steps` defaults to `[]`
  (definition.py:194), so for a python row (phase 04's summary `definition` with a `tools` list, the
  same shape `probes.collect_tools` reads) it adds nothing and the delegate would lose the tools the
  playbook calls. Add one branch: `definition.get("format") == "python"` → `sorted(set(definition.get("tools") or []))`.
  The allowlist policy (`delegate_toolset` :170-193: authoring tools + list/status + referenced tools,
  never `send_chat_message`) is unchanged — master §2 Sub-agents "delegate toolset unchanged" (Risks 11).

Author stamping (no DDL — `PlaybookVersion.author` exists: `String(64)`, default `"owner"`,
models.py:98; `delegation:<uuid>` is 47 chars):
- `_edit_impl` write part 2 mints with `author="agent"` (agent_tools.py:1871-1877 at 8c31a60; :2333 at phase 04's `17bbd47`) →
  `author=writer_identity()`.
- `_propose`'s v1 row mint (added by plugin/04 with `author="agent", message="candidate"`, agent_tools.py:539 at `17bbd47`; used for both the new-row and the re-create path) → `writer_identity()`.
- `_manifest_set` (:2005-2011 at 8c31a60; `_manifest_set` :2456, `author="agent"` :2473 at `17bbd47`) → `writer_identity()`.
- `mint_version` (versioning.py:122-169, `author=author` :153) unchanged. `Playbook.created_by`
  (`String(32)`, models.py:73; set at agent_tools.py:311/:325) stays `"agent"` — too short for a
  delegation id and not a version row.
- Readers already emit the column: `_versions` entries `"author": r.author` (:1156), `_version_read`
  (:1220), routes `list_versions` (:1102), `get_version` (:1158); `playbook_overview`'s candidate
  block `{version, author, saved_at, last_test_run}` (plugin/09, master §2 Result provenance) reads
  the same row. No reader change; the new tests pin the values.

Candidate-conflict guard (`plugin_playbooks/versioning.py`, new
`async def candidate_conflict(session, playbook, author) -> dict | None`):
- Returns `None` when `playbook.candidate_version` is unset or the candidate row
  (`get_version_row` :39-54) has `author == author`. Otherwise
  `{"candidate_version": N, "author": <row.author>, "saved_at": <row.created_at isoformat>}`.
- Author labels in messages: `agent` → "the agent", `owner` → "the owner",
  `delegation:<id>` → "delegation <first 8 hex chars> (delegation:<id>)".
- `_edit_impl` READ header (:1717-1738 at 8c31a60; `_edit_impl` :2027 at `17bbd47`, rejected-write payloads :2153/:2233, `_EDIT_PAYLOAD_PROPS` :2400, `playbook_edit` ToolDef :2419) gains `"candidate_author"` and, when the guard fires,
  `"conflict": {...}` plus a first instruction line "Another author's candidate exists — do not
  write; ask the owner".
- `_edit_impl` WRITE part 2 under the lock (:1847-1861) calls the guard after the version-race
  check (:1853-1858) and BEFORE `_check_ticket(..., consume=True)` (:1859). Refusal payload
  (plugin/04's edit-error shape): `{"stage": "write", "saved": false, "error": "Playbook '<name>'
  already has an unpublished candidate v<N> saved by <label> at <saved_at>. Nothing was saved — a
  candidate written by someone else is never replaced silently. Ask the owner whether to publish it
  or replace it; then re-read and retry.", "conflict": {...}, "ticket": <same>,
  "ticket_still_valid": true}`.
- Same-author re-save keeps today's pointer move (tests/test_candidate_flow.py:191-207 stays green).
- `_propose` re-create over an archived name with a candidate (:293-312): same refusal shape minus
  the ticket keys.
- `_manifest_set` (:1991-2019 at 8c31a60; :2456 at `17bbd47`): the guard applies once the P0 plan
  `2026-09-06-manifest-set-live-bypass` makes it a candidate writer; at HEAD it writes live (:2012 at 8c31a60; :2476 at `17bbd47`)
  and only the author stamp changes here.
- Explicit replace (assumption, Risks 5): `playbook_edit` gains an optional `replace_candidate: bool`
  (default false; `_EDIT_PAYLOAD_PROPS` :1942-1954; description "OWNER-authorised only — pass true
  only after the owner said to replace <author>'s candidate"). With it the write proceeds and the
  minted row's `message` is `"candidate (replaced <author> v<N> on owner instruction)"`. No override
  on propose or manifest_set.

Delegate prompt v2 variant (`_delegate_prompt(task, pb, *, format=None)`, delegation.py:208-393):
- 2026-09-08 (phase 08): the v2 prompt must teach the per-run card as landed — a `playbook_run` result with `status: "parked"` + `approval_id` means an owner card is pending: report it, END the turn, never re-run, never call `playbook_set_autonomy` to get past it (the tool's own result now says the change is PERMANENT); a `playbook_publish` refusal naming a parked candidate run means wait for the owner's decision, never start a second candidate run; a done python run reports `result` (its `run()` return) beside `step_results`; a failed run's `playbook_status` hint names `playbook_dry_run(..., stubs_from_run='<run_id>')` — the prompt's fix loop should use it before a second real candidate run; the delegate's status text for a parked run is phase 07's `parked on approval #<uuid> — nothing to poll …` line.
- Format resolution: `format or getattr(pb, "format", None) or "python"` — a new playbook is python
  (plugin/04's propose default); an edit job follows the target's column. `_playbook_agent` (:764)
  passes nothing extra; the pblang variant is selected only by a pblang target or an explicit
  `format="pblang"`.
- Same 11 `## N.` headers (comment :196-199; pin `tests/test_delegate_prompt.py:26-29`). Shared
  text: sections 1, 3, 6, 7, 10, 11 (format-neutral today).
- Python §2 brief: adds "Format: python (v2)" under the task.
- Python §4 work loop (six steps): 1 ORIENT — for an edit job `playbook_edit(name)` READ stage
  returns the code; never `playbook_language_reference` (pblang-only, :255-260). 2 WRITE —
  `playbook_propose(format="python", ...)` or the edit write; a green write is `validated: true`
  ("Do not call playbook_validate"); a red write returns the checker issues with the ticket still
  valid — fix and re-save, cap 3 failed writes (replaces OUTLINE :261-268 and VALIDATE :269-272).
  3 DRY-RUN — `playbook_dry_run` → `status: simulated`, `stubs=`, `unreached_call_sites` empty or
  explained (:273-277 reworded). 4 PREFLIGHT (:283-285). 5 PROOF RUN — `playbook_run_candidate`,
  real side effects (:286-288). 6 PUBLISH (:289).
- Python §5 quality bar: the v2 language rules — `V2_SKILL_BODY` from `plugin_playbooks/v2/skill.py`
  (plugin/05) pasted verbatim, replacing the pblang shapes :291-307 (assumption, Risks 4).
- Python §8 worked shapes: the master §2 example `async def run(ctx, inputs)` (phase 04's `PY_CODE`)
  with a BAD → GOOD pair (BAD: `playbook_validate` after a green write / a dry run reported as a
  real run; GOOD: dry run → candidate run → publish), replacing :334-366.
- Python §9 checklist: item 1 (:371) becomes "the last write returned validated: true"; the closing
  "Then playbook_publish(name, explanation=...)" (:379) stays so `test_checklist_wired_before_publish`
  (:63-73) holds for both. Budgets (:309-320): "3 failed validates" → "3 failed writes", python only.
- Tail: `_PROMPT_TAIL` (:200-205) says "The reference tool, not memory, is the source of pblang
  syntax." — pblang only. New `_PROMPT_TAIL_V2`, same 3-5 lines, last sentence "The v2 rules above,
  not memory, are the source of the ctx.* contract." The pblang variant is byte-identical to the
  phase-00 text.
- Steering text: `_DELEGATION_SKILL_BODY` (`__init__.py:566-569`, "read, edit, validate, dry-run,
  … publish") → "read, write (validated on save), dry-run, real candidate run, publish"; the
  example task (:596-597) says "publish when the candidate run is green". Size pin
  `len(skill.body) < 2560` (tests/test_delegation.py:412) holds. `playbook_agent` ToolDef description
  (delegation.py:802-811) adds one sentence: "New playbooks are written as python (v2); an edit
  follows the target's format."

Versions UI (`ui-src/src/playbooks/VersionsTab.tsx`):
- `authorLabel` (:179-181) maps `agent` → "agent", `owner` → "you", `system` → "system",
  `delegation:<id>` → "delegation <first 8 chars>" with `title={author}` carrying the full id;
  empty → "—". Rendered on the row meta line (:662) as today, wrapped in
  `<span data-testid={`version-author-${v.version}`}>`. `types.ts` `VersionDetail.author`
  (:140-145) unchanged.
- Rebuild: `cd ui-src && npm ci && npm test && npm run build`; commit the hashed pair under
  `plugin_playbooks/ui/assets/` and `ui/index.html:7-8` (replaces `index-BqhDbTui.js` /
  `index-BgLNZxTK.css` or whatever phase 10 left).

Tests and stamps:
- New `tests/test_v2_delegation.py`, `tests/test_v2_end_to_end.py`; edits to
  `tests/test_delegate_prompt.py` (explicit `format="pblang"` on the `pb=None` calls so its pins keep
  pinning the pblang variant) and `ui-src/src/playbooks/__tests__/VersionsTab.test.tsx`.
- Minor bump in the three stamps (`pyproject.toml:3`, `plugin_playbooks/luna-plugin.toml:2`,
  `plugin_playbooks/__init__.py:647` at phase 04's `17bbd47`; 0.48.0 after phase 04, 0.49.0 planned after phase 05 — the exact number is whatever phases
  06-10 left plus one minor): the UI bundle and a ToolDef payload change. The `playbook_agent`
  `[[tools]]` entry in `luna-plugin.toml` is re-written by hand to match the new description
  (`tests/test_manifest_drift.py:50-60` only compares names, policy, risk_level and count, so a stale
  description would not fail — the manifest text is the marketplace listing); tool count stays at
  phase 09's 26, tables at 11. One commit on `v2-runtime`, not pushed.

## Not in this phase
- The delegate toolset policy (`delegate_toolset` :170-193, `AUTHORING_TOOLS` `__init__.py:880-902`) —
  unchanged by master §2 Sub-agents; the only touch is the python-summary branch in
  `_referenced_tools` (Scope). No `format` parameter on `playbook_agent`.
- `ctx.agent` transcript capture inside v2 runs (master §2 Sub-agents, first bullet) — plugin/03.
  The nested-run guard (`_nested_run_refusal` over `_active_playbook_run`, the alias of
  `runner.active_run_id` — agent_tools.py:46, :80-100) — plugin/02/03.
- Owner write paths (routes.py `update_playbook` :770-812, `put_manifest` :1521-1547) write live
  versions, not candidates, so they never "save over" a candidate; untouched. `playbook_manifest_set`
  becoming a candidate writer — P0 plan `2026-09-06-manifest-set-live-bypass`.
- `playbook_overview` itself (plugin/09), the canvas (plugin/10), the graders and the gate run
  (dojop/02 — this phase supplies the build and records the verdict), the final measurement
  (plugin/12, dojop/03).
- The delegation card token flow (master §1.13) and `_TranscriptFeed` (plugin/03) — untouched.

## Steps
1. Baseline. Record HEAD, `uv run pytest tests -q` and `cd ui-src && npm test` results after
   phase 10 in the summary; re-map this file's delegation.py citations
   (`grep -n "^## \|_PROMPT_TAIL\|Eleven" plugin_playbooks/delegation.py`) after phase 00's
   deletions. Proof: the re-mapped lines are in the summary.
2. Identity. Add `_delegation_id` + `writer_identity()` to delegation.py; set/reset in
   `_drive_delegation` around `run_turn` (:593-603; reset in the `finally` :637).
   Proof: `tests/test_v2_delegation.py::test_writer_identity_default_is_agent` and
   `::test_identity_set_inside_run_turn` (a `ToolCallingAgent` fake whose `run_turn` calls
   `writer_identity()` and returns it → `delegation:<row.id>`).
3. Stamp. Replace `author="agent"` at the three mint sites (`_edit_impl` :1871-1877, `_propose`'s v1 mint from plugin/04, `_manifest_set` :2005-2011) with `writer_identity()`.
   Proof: `::test_delegated_edit_stamps_delegation_author` — `playbook_agent` from `build_delegation_tools(FakeCtx(FakeAgent(...)), sf, AUTHORING)` (tests/test_delegation.py:85-122 — `FakeCtx` carries `.agent` and `.current_conversation_id`, which `_playbook_agent` :725 reads) drives `_drive_delegation`; the fake agent's `run_turn` calls the real `playbook_edit` handler (READ then WRITE) from `build_tools(sf, _Bus(), runner, _Ctx(_Approvals()))` (the lifecycle repro's `_Ctx`, a different object from `FakeCtx`); the new row's `author == f"delegation:{row.id}"`; `_versions` and REST `list_versions` (client fixture as tests/test_version_routes.py:33-45) echo it; an inline (non-delegated) edit stamps `agent`. `::test_delegate_toolset_reads_python_summary`: `delegate_toolset` on a row whose `definition` is `{"name": ..., "format": "python", "tools": ["file_write"]}` includes `file_write`; a pblang row behaves as today.
4. Guard. Add `candidate_conflict` to versioning.py; wire the READ header, the WRITE refusal before ticket consumption (:1859), the propose re-create path, and `replace_candidate`.
   Proof: `::test_conflict_guard_names_the_author` (candidate v2 by `delegation:<uuid>`, inline edit as `agent` → error names "delegation <8 chars>" and "v2", `saved is False`, `ticket_still_valid is True`; DB: `candidate_version == 2`, version-row count unchanged, the ticket still works on a retry); `::test_same_author_resave_moves_pointer` (author `agent` twice → 2 → 3, old row stays); `::test_read_header_warns_before_write`; `::test_replace_candidate_is_explicit` (message carries the replaced author); `::test_propose_recreate_refuses_foreign_candidate`.
5. Prompt. Add the `format` keyword, the python branches and `_PROMPT_TAIL_V2`; edit `tests/test_delegate_prompt.py` so every `pb=None` call passes `format="pblang"` (module helper `_p(task, pb=None)`), assertions untouched.
   Proof: `tests/test_delegate_prompt.py` green (pblang variant unchanged); `::test_v2_prompt_eleven_sections_in_order` for `(task, None)`, `(task, pb_python)` and `(task, pb_pblang)`; `::test_v2_prompt_language_rules` (python variant contains `async def run(ctx, inputs)`, "Do not call playbook_validate", `playbook_dry_run`, `playbook_run_candidate`, `V2_SKILL_BODY` verbatim; contains neither `playbook_language_reference` nor `collect=`; ends with `_PROMPT_TAIL_V2`; ≤ 5 shouty lines as :90-97; length ≤ `len(pblang variant) + V2_SKILL_MAX_BYTES`); `::test_prompt_follows_target_format` (`_playbook_agent` on a python row → the prompt handed to `FakeAgent.run_turn` carries the v2 marker; a pblang row → `playbook_language_reference`).
6. Steering text. Reword `_DELEGATION_SKILL_BODY` and the `playbook_agent` description.
   Proof: `tests/test_delegation.py::test_delegation_tools_are_skill_gated_and_chat_only` (:400-412)
   and `::test_skill_descriptions_steer_playbook_jobs_to_delegation` (:415-429) green;
   `::test_delegation_skill_names_v2_loop` (new file) asserts "validated on save" and "real
   candidate run" and no "validate," step word.
7. UI. Change `authorLabel`, add the testid, add `VersionsTab.test.tsx::renders author labels`
   (entries with `author: 'agent' | 'owner' | 'delegation:1234abcd-…'` → "agent" / "you" /
   "delegation 1234abcd" with the full id in `title`). Rebuild and commit the bundle.
   Proof: `npm test` green; `git status` shows the new hashed pair and `ui/index.html`; the old pair
   deleted.
8. End-to-end script, `tests/test_v2_end_to_end.py`. Harness: aiosqlite engine; `tools = build_tools(sf, _Bus(), runner, _Ctx(_Approvals()))` with `_Ctx` / `_Approvals` from tests/test_repro_fixplaybooks_lifecycle.py:54-87 (phase 04's `tests/v2harness.py::env(*, script=None, decision="approved", **fake_tools)` builds exactly this — a real `PlaybookRunner` with the scripted `code_run` plus named fake tools, `_ensure_columns` applied — and may be reused) (a bare `ctx=None` skips the card — agent_tools.py:2088-2093 — so the ctx must carry `.approval`; `_Approvals` records both `request` and `request_nowait`, the two the publish gate may use, :2190); `runner = PlaybookRunner(session_factory=sf, tool_registry=<registry with a recording file_write fake AND the scripted code_run fake>, events=_Bus())` with the scripted `code_run` fake of tests/test_v2_loop.py so the candidate run executes the python for real (no jail) — `code_run` must be in the registry because phase 04's `collect_tools` adds it to the probe list and a missing tool is a `failed` probe, which blocks publish. The script's playbook is NOT the master §2 example (`PY_CODE` calls `ctx.approve`, which would raise a second card and break the one-card assertion, and its tools are `fetch_list`/`send_message`): `E2E_CODE` is a two-line `async def run(ctx, inputs)` that awaits `ctx.tool("file_write", path=inputs["path"], content=inputs["note"])` and returns `{"written": inputs["path"]}` — no `ctx.approve`, no `ctx.llm`. A `ScriptedAgent` runs the "turn" as an ordered list of handler calls under `_delegation_id.set(uuid)`, recording `(name, result)`:
   - `playbook_propose(name="pb-e2e", format="python", code=E2E_CODE, inputs_schema=...)` → `status == "candidate_saved"`, `validated is True` (phase 04's propose result at `17bbd47` has no `next` key — keys `playbook_id, name, format, status, live_version, candidate_version, runnable_via, triggers_active, publish_required, validated, warnings`; plugin/09 adds `next`, and if it exists by then it must not name `playbook_validate`).
   - `playbook_dry_run` → `status == "simulated"`, zero `playbook_runs` rows.
   - `playbook_run_candidate(name, inputs=..., wait_seconds=30)` (the handler waits via `runner.wait_for_run`, :2698-2701 at 8c31a60; `_run_candidate` :3101 at `17bbd47`; default `_RUN_WAIT_DEFAULT` 55 s, :456 at 8c31a60) → `status == "done"`, one run row `is_test=True`, `trigger="agent-candidate"` (:2699), the fake `file_write` was called with the real args.
   - `playbook_publish(name, explanation=EXPLANATION)` → `status == "published"`, `live_version == 1`, `candidate_version is None`, exactly one entry in `approvals.requests`, the live row's `author == "delegation:<uuid>"` — publish moves the pointer onto the candidate row (`_apply_version_to_live` :2454-2456, no new row minted), so the live row is the delegated row.
   - Across the script: the recorded call list has no `playbook_validate` (none after the green write, none at all); no result's `next` asks for validation. Rejected twin: `_Approvals(decision="rejected")` → nothing published, `live_version` None, still exactly one card.
   Proof: the file is green.
9. Stamps, manifest, suite, commit. Bump the three stamps; update the `playbook_agent` `[[tools]]`
   description in `luna-plugin.toml` by hand (Scope — no generator, the drift test does not check it);
   `uv run pytest tests -q`, `cd ui-src && npm test`, `uvx ruff check --select F401`; one commit
   "032/11: delegation v2, author stamping, conflict guard, e2e" on `v2-runtime` (local only).
   Proof: `tests/test_manifest_drift.py` green (26 tools, 11 tables, stamps agree); HEAD after in
   the summary.
10. Integration proof on a `vaselin-*` agent (side-load per phase 05's recipe,
    `LUNA_PLUGIN_SET_DIR`; version proof `run.json.plugin_versions["plugin-playbooks"]` == this
    stamp): delegate one edit on a throwaway python playbook;
    `GET /api/p/plugin-playbooks/playbooks/<name>/versions` shows `author: delegation:<id>` and the
    versions tab renders "delegation <8 chars>"; then run one existing pblang (v1) playbook end to
    end via `playbook_run` (agent_tools.py:568 `ToolDef(name="playbook_run", ...)`) on the
    side-loaded build and record its run id and status `done` in the summary — the master §4
    Rollout standing regression gate ("v1 playbooks on that agent must keep working at every
    phase"). Proof: the row, a screenshot reference and the v1 run id + status in the summary.
    Red here (author `agent` on a delegated write) = the ContextVar did not propagate → STOP,
    re-plan (Risks 2); a v1 run that is not `done` is also STOP (regression on the build dojop/02
    is about to measure).
11. Gate. Hand the build to dojop/02 (its phase file `phases/02-criteria-graders-and-keyhole-gate/PLAN.md` steps 7-9 — pre-flight, `-- --trials 2 --tags keyhole`, verdict; the repo plan "## 02" numbers the same work as steps 5-6; verdict against the per-task baseline 0056 `dryrun-is-not-a-run`; 0057 `comment-lies-in-code`, `docs-say-verified`, `docs-say-slow-queue`, `edit-not-in-run`; 0058 `note-says-broken`, `run-history-failure`, each 2/2). In the same session run `python run.py run --base <url> --ids playbooks.candidate-then-publish --trials 2` (turn 2 `must_not_call: [playbook_publish]`, turn 3 publish) as the live half of the end-to-end story (its YAML grades `must_call_any` propose/edit/publish, `must_not_call` on turn 2 and judge dims only — it does not grade a dry run or a candidate run; those steps are proven by Step 8, Risks 8). Copy dojop/02's verdict block (results folder id, trials, per-task pass^k, failing checks, `must_not_call` failures, code-graded wrong-claim count from `claim_matches_runs`) into this summary and luna-fixer M4's. STOP RULE: any miss → `VERDICT: stop`; plugin/12 does not start.
    Proof: the block exists in both summaries.
12. Summary and revisions. Write `execution_summary.md`; revise plugin/12 (author values it will see
    on migrated rows; the stamp) and luna-fixer M4/M5 where facts changed.

## Exit tests
- `tests/test_v2_delegation.py`: `test_conflict_guard_names_the_author` (error names the author
  label and version; `saved is False`; `ticket_still_valid is True`; pointer and row count
  unchanged); `test_same_author_resave_moves_pointer`; `test_replace_candidate_is_explicit`;
  `test_propose_recreate_refuses_foreign_candidate`; `test_delegated_edit_stamps_delegation_author`
  (row `author == "delegation:<id>"`; `_versions` and REST `list_versions` echo it);
  `test_writer_identity_default_is_agent`; `test_delegate_toolset_reads_python_summary` (a python
  row's `definition["tools"]` reaches the allowlist; `send_chat_message` still excluded);
  `test_v2_prompt_eleven_sections_in_order` (headers
  `["1".."11"]` for python and pblang); `test_v2_prompt_language_rules`;
  `test_prompt_follows_target_format`; `test_delegation_skill_names_v2_loop`.
- `tests/test_delegate_prompt.py`: all nine tests green with the explicit pblang format —
  `test_eleven_sections_in_order` (:26-29) unchanged in substance.
- `tests/test_delegation.py`: green unchanged (toolset, `max_turns` 40, `memory_write` False, card
  token, skill size < 2560).
- `tests/test_v2_end_to_end.py`: `test_agent_turn_proposes_dry_runs_runs_and_publishes` (no
  `playbook_validate` call after the green write; `file_write` really called in the candidate run;
  exactly one approval request; `live_version == 1`, `candidate_version is None`; live row author
  `delegation:<id>`); `test_rejected_card_publishes_nothing`.
- `ui-src/src/playbooks/__tests__/VersionsTab.test.tsx::renders author labels`; `npm test` green;
  bundle rebuilt and committed.
- `tests/test_manifest_drift.py`: 26 tools, 11 tables, three stamps agree at the new minor.
- Integration proof (Step 10) recorded: a delegated write stamped `delegation:<id>` on the
  `vaselin-*` build; and one existing pblang (v1) playbook run end to end via `playbook_run` on
  the same side-loaded build, its run id and status `done` recorded in the summary (master §4
  Rollout standing regression gate).
- Keyhole gate verdict (dojop/02, Step 11) recorded: 7 tasks in one run, `--trials ≥ 2`,
  pass^k ≥ 1.00, zero failed code checks, zero `must_not_call` failures, wrong-claim count
  code-graded; `candidate-then-publish` 2/2 with no publish on turn 2. Red → `VERDICT: stop`.
- Existing suite green (`uv run pytest tests -q`); `uvx ruff check --select F401` clean.

## Cross-repo checks
- dojop/02 (dojoP `plans/0002-fix-playbooks-bench/PLAN.md` "## 02 — Criteria graders and keyhole
  gate"): consumes this phase's build (its step 5), grades wrong claims with `claim_matches_runs`
  against run rows (its step 1; the judge sees `str(result)[:300]`, `lib/judge.py:124`), returns
  the verdict copied here (its step 7). dojoP commits to `main` and pushes to origin
  novalystrix-org/dojoP per its plan; this repo does not push.
- plugin/04 (landed `17bbd47`): `_propose` returns `candidate_saved` with `validated: true` (no `next` key — see Step 8);
  the edit-error shape `{stage: "write", saved: false, format, errors, warnings, ticket, ticket_still_valid: true, expires_in_seconds, retry}` (+ `language_reference` and a compat `error` key on pblang compile errors; the format-change refusal is the shorter `{stage, saved, format, error, ticket, ticket_still_valid}`) — the guard's refusal reuses the shorter shape; the delegate
  toolset includes `playbook_propose` (`__init__.py:880-884` at 8c31a60; `AUTHORING_TOOLS` :897 at `17bbd47`), so propose = candidate is what closes
  the delegation side door (master §2 Lifecycle). `_referenced_tools` :141, `delegate_toolset` :165, `_PROMPT_TAIL` :195, `_delegate_prompt` :203, `_drive_delegation` :583, `_playbook_agent` :713 at `17bbd47` (delegation.py untouched by phase 04).
- plugin/05: `V2_SKILL_BODY` / `V2_SKILL_MAX_BYTES` (`plugin_playbooks/v2/skill.py`) are imported by
  the v2 prompt; the owner-intent-to-publish sentence in `playbook_publish`'s description stays the
  delegate's rule too.
- plugin/09: `playbook_overview`'s candidate `author` shows `delegation:<id>` after a delegated
  save — one assertion added to `tests/test_v2_overview.py` if the fixture exists there, else in
  `tests/test_v2_delegation.py`.
- luna `fix-playbooks` @ f05bdf2 (tip now 5a92c05, the plan-writing commit; no code change): no luna
  change. The ContextVar relies on tool handlers executing inside the `run_turn` chain — `ctx.agent`
  is `PluginAgentFacade.run_turn` (`luna/plugins/agent_facade.py:117`), which awaits the agent's turn
  in the caller's task, and the tool loop awaits each handler at `luna/agent/runtime.py:2309`
  (`asyncio.wait_for(_handler(**call_kwargs), ...)`, confirmed at f05bdf2); tasks the turn creates
  after the `set()` inherit a copy of the context. Step 10 proves it on the real core.
  The delegation card token flow (master §1.13) is untouched.
- luna-fixer M4: the M4 exit-test lines "Conflict guard test; scripted end-to-end …" and "Keyhole
  gate (dojop/02)" are satisfied by this file's exit tests; the verdict block goes into M4's summary.

## Risks and open questions
1. Dependency line. The repo index says this phase depends on 04, 08, dojop/02; dojoP's plan says
   its phase 02 depends on "plugin/11 build"; luna-fixer M4's table says plugin/09, plugin/10.
   Reading used here: code (Steps 1-9) after 04/08/09/10; dojop/02's run after Step 9; its verdict
   closes this phase. Flagged, not resolved.
2. Assumption: a ContextVar set in `_drive_delegation` around `run_turn` reaches the plugin's tool
   handlers on the real core. The unit tests prove it only through a fake agent; Step 10 is the real
   proof. If red, the alternative is a luna-side passthrough (a `ctx.delegation_id` on the plugin
   API) — a luna/03-class change, re-planned, not improvised.
3. Assumption: `_delegate_prompt(task, None)` now yields the python variant (new playbooks are
   python per plugin/04). `tests/test_delegate_prompt.py`'s `pb=None` pins (:42-50, :84-87,
   :100-104) therefore pass `format="pblang"` explicitly; no assertion is weakened.
4. Assumption: the delegate's source of v2 rules is `V2_SKILL_BODY` pasted into §5 of the v2 prompt
   (the delegate is headless — no skill loads, and `playbook_language_reference` is pblang-only).
   Bounded by `V2_SKILL_MAX_BYTES` (6144), so the v2 prompt stays under the v1 prompt plus 6 KB; the
   just-in-time rule (:42-50) remains a pblang-variant pin. The master is silent on the mechanism.
5. Assumption: an explicit `replace_candidate` flag on `playbook_edit` is the "not silent" replace
   the master implies; without it a foreign candidate can only be resolved by the owner publishing
   it (no discard-candidate surface exists at HEAD). Owner call at execution; dropping the flag
   removes one test and one manifest change.
6. `_manifest_set` is guarded only once the P0 manifest-set plan lands; at HEAD it writes live
   (:2012) and the guard would never fire. `Playbook.created_by` (`String(32)`) keeps `"agent"`.
7. Line drift: phase 00 deletes the SPECS step (:278-282) and renumbers 6/7/8 → 5/6/7, drops
   checklist item 2 (:370-379) and the "3 failed spec" assert (`test_delegate_prompt.py:80`);
   phase 04 did NOT rewrite the propose sentence (delegation.py:261 at `17bbd47`, "Create with playbook_propose (pass manifest=)" — it never claimed a live create; phase 04 summary Deviations 8), so this phase's python §4 replaces it; phase 09 adds a tool. All delegation.py /
   agent_tools.py citations above are at 8c31a60 — symbols are the anchor.
8. The end-to-end script calls handlers directly, so the core's `prompt_always` gate on
   `playbook_run_candidate` (tests/test_candidate_flow.py:419-420) is not exercised there; the
   publish card is the `_Approvals` stub. The live half is `candidate-then-publish` on the bench
   (Step 11), which is not a keyhole task and runs by `--ids` in the same session — an addition to
   dojop/02's `--tags keyhole` run, flagged for the dojoP operator.
9. The version number is fixed at execution (phases 06-10 may bump); the minor bump here is
   mandatory (UI bundle + ToolDef description).
10. Standing constraints: nothing is published, pushed to main or promoted — plugin-playbooks and
    luna commits stay on their local branches; only `vaselin-*` machines are touched for Step 10
    and the bench target; secrets never committed or printed; specs stay removed (owner
    2026-09-07); dry run, `test_run` gate and probes stay; `ctx.sleep` deferred.
11. Assumption: the `_referenced_tools` python branch (Scope, Writer identity) is the master's
    "delegate toolset unchanged" applied to a v2 target — the same allowlist rule fed from the
    summary `definition` instead of step rows. The master does not mention the helper; without the
    branch a delegated edit on a python playbook has none of the playbook's tools in its allowlist.
    Dropping it removes one test and no other change.
12. The e2e script's `E2E_CODE` is not the master §2 example (Step 8): the example's `ctx.approve`
    raises a card of its own (plugin/03's in-process form), which would make "exactly one approval
    request" false for reasons unrelated to publish gating. The master's example still appears in
    the v2 prompt §8 (from phase 04's `PY_CODE`).

## Execution summary
Written to execution_summary.md in this folder after the phase runs, using this template:
- Ran: (commands, dates, HEAD before/after, results folder ids)
- Results: (every exit test with its outcome and the relevant output; anything red and why)
- Deviations from this plan: (what changed and why)
- Learned: (facts that change later phases)
- Revised: (which later phase files were edited because of this, and how)

## Post-M3 checklist (2026-09-08, luna-fixer M3 gate GREEN — `plans/2026-09-06-fix-playbooks/phases/M3-lifecycle-parity/execution_summary.md`)
- Starting point: after plugin/09 + plugin/10 on `v2-runtime`; M3 handed over `b340d2c` (docs) over code `3a1641c` = plugin/08, stamps **0.52.0** ×3 (`pyproject.toml:3`, `plugin_playbooks/luna-plugin.toml:2`, `plugin_playbooks/__init__.py:723`) — expect 0.53.0 from plugin/09 by the time this phase starts (re-check the three stamps + `tests/test_manifest_drift.py`). ` M uv.lock` pre-existing: never staged, never discarded.
- Suite baseline at M3: `7 failed, 658 passed` (665 collected, ~80 s) — plus whatever 09/10 add. The 7 red = the repro pins by name (`tests/test_repro_fixplaybooks_lifecycle.py::{test_publish_success_carries_verified_readback, test_approved_then_regated_same_payload_trips_loop_guard, test_manifest_set_does_not_flip_live}`, `tests/test_repro_fixplaybooks_runtime.py::{test_interrupted_run_survives_restart_instead_of_failing, test_wait_for_approval_actually_gates, test_wait_for_event_actually_waits, test_tool_step_timeout_is_enforced}`); flip none, never skip/xfail, never weaken; set at exit = set at entry.
- Rules: relative imports only + `tests/test_loader_style_import.py` green before every commit; any test driving more than one run task at once (the end-to-end script will) uses the file-backed `db_file(tmp_path)` fixture of `tests/test_v2_resume.py`, never the in-memory `StaticPool` engine, no DB reads under a live run task (observe bus events); `tests/test_v2_parity.py` (18 — incl. `test_delegation_card_token_flow_for_python_playbook`, which pins the delegation card body as a superset carrying `waiting_for_approval`) and `tests/test_v2_live_version_invariant.py` (2) stay green; ruff F401 clean; commits local only, never push.
- What phase 08 landed that this phase consumes (dated lines at "Depends on" :5 and "Delegate prompt v2 variant" :77): the per-run card (`kind="playbook_run"`, payload `{playbook, version, inputs}`) raised in `start_run_background` BEFORE any task spawn — an `agent_must_confirm` run returns `{run_id, playbook, status:"parked", approval_id:<str UUID>, message:"run <id> waiting on owner card #<uuid> — tell the user; nothing to poll"}` and the delegate prompt must say: report it and end the turn, never call `playbook_set_autonomy` (which now states permanence in its `note`), never poll; `manual_only` → `{status:"refused", playbook, reason}`; `parked_on.gate == "run"`; test-run cards labelled "test run of candidate vN" (`presentation["eyebrow"]` + summary prefix); the publish gate names a parked candidate run — the delegate waits, never starts a second candidate run; `result` in run results and as the 13th (last) key of `playbook.run.completed`; the `stubs_from_run=` fix loop on `playbook_dry_run` (`stubs_source` only when accepted; per-occurrence ids `id#n`); `timed_out_unknown` is a failure (`OutcomeUnknown`), `parked` never is. `KNOWN_SIDE_DOORS = {agent_tools.py::_manifest_set}` stays until plan `2026-09-06-manifest-set-live-bypass` lands.
- Owner questions still open (record, do not resolve): phase 07 Risks 10 option (b) — a child parking inside `ctx.subtask` fails the parent's effect (the delegate prompt must not promise otherwise); template-rendering parity; stamp bump.
- From M1 (M2 item 16): `luna_version "?"` in dojoP `run.json` (`dojoP/run.py:70-75`) and delegation Risk B1 untested — this phase's end-to-end script + dojop/02 pick them up.
- Cross-repo: luna `fix-playbooks` @ `7d2ac9d` (0.92.044) → luna/03 lands 0.92.045 after plugin/09; core admits `playbook_effect` + `playbook_run` (`PLAYBOOK_RESUME_KINDS`, `luna/approval/contract.py:25`); cross-check recipe `LUNA_PLUGIN_SET_DIR=<scratch>/m2/plugin-set` → 107 + 007.009 `1 failed, 34 passed` (pre-existing 007.009 red, luna/04). dojoP `main` @ `ece7136` (run 0061 = regression floor); dojop/02's contract notes carry the parked/refused shapes.
