# 032 — Phase 03: Effects: ctx.llm, ctx.agent, ctx.subtask, ctx.gather, ctx.approve (in-process form)
Status: pending
Master: /Users/roy/Documents/my-projects-docs/luna-fixer/plans/2026-09-06-fix-playbooks/PLAN.md — §2 Language, §2 Execution model, §2 Effect execution semantics, §2 Sub-agents, §2 Lifecycle (`result` column, `failed_handled` hoisting), §3 P1; master phase M1
Repo / branch: luna-plugins/plugins/plugin-playbooks, branch `v2-runtime` (HEAD 8c31a60 at writing; origin/main 749f126; version 0.46.0). Read-only reference: luna branch `fix-playbooks` @ f05bdf2 (approval engine, agent facade). Commits stay local on `v2-runtime`; nothing is pushed or published.
Depends on: plugin/01 (checker admits `ctx.llm/agent/subtask/gather/approve`, rejects `ctx.wait_event` and `ctx.sleep`), plugin/02 (host segment loop in `v2/loop.py`, in-memory `JournalStore` in `v2/journal.py`, shim exit/replay in `v2/shim.py`, `ctx.tool`, the `ctx.EffectError` family)
Unblocks: plugin/04, plugin/05 (dry stubs for these kinds), plugin/07 (park form of `ctx.approve`), plugin/08 (persisted subtask `result`, `failed_handled` hoisting exclusion)

## Goal

Give the v2 loop the four remaining effect kinds and the batching primitive
with v1-parity execution: `ctx.llm` and `ctx.agent` go through the same
`PluginAgent` facade v1 uses (`run_llm`/`run_turn`), record cost the way
v1 does and attach the sub-agent transcript to the journal entry;
`ctx.subtask` runs a child playbook through the same loop with parent/child
run rows, a cycle guard and the child's return value handed back in
memory; `ctx.gather` lets the shim exit with N pending effects that the
host runs concurrently and replays in argument order; `ctx.approve` is a
real card raised through an awaited in-process `approvals.request` that
blocks the run task and surfaces reject/expiry as catchable exceptions.
A caught effect failure is journaled `failed_handled` and the run completes.

## Scope — changes

All effect execution lives host-side in `plugin_playbooks/v2/loop.py`
(phase 02's module; if phase 02's summary split effect dispatch into its
own module, the handlers go there). The shim side is `v2/shim.py`.

- `ctx.llm(prompt, output=None, purpose=None, model=None, system=None)`.
  Host calls `self._agent.run_llm(prompt, purpose=purpose or
  "summarization", model=model, system=system, output_schema=output)` —
  the exact call `_run_llm_step` makes (runner.py:1008-1014) minus v1's
  template rendering (v2 code builds its own strings). Missing agent →
  entry `failed` with `EffectError("ctx.llm requires an injected agent")`
  (v1 raises the same shape at runner.py:950-955). The facade is the one
  `__init__.py:747-753` injects (`agent=ctx.agent`); its contract is
  luna `plugins/agent_facade.py:67-78` (`run_llm(prompt, *, purpose,
  model, system, output_schema, temperature, max_tokens) ->
  tuple[dict|str, Any]`). Return rule (master §2 Language): `output=`
  given → the dict (a non-dict answer is wrapped `{"_raw": result}`, the
  v1 rule at runner.py:1018); no `output=` → `str` (a dict answer is
  `json.dumps`-ed). Cost: the entry gets `cost_cents` under the
  `_record_step_cost` rule (runner.py:1382-1397: only when `usage` has a
  truthy `cost_cents` attribute). Billing scope: the call runs inside
  `_playbook_origin_scope(playbook)` (runner.py:173-189), as v1's
  `_drive_run` does at runner.py:489-490.
- `ctx.agent(prompt, output=None, tools=None)`. Host calls
  `self._agent.run_turn(prompt, output_schema=output, tools=tools,
  memory_write=False, conversation_id=run.report_to,
  event_stream_handler=feed.handle)` — v1's call at runner.py:962-968
  (`ctx.conversation_id` is `run.report_to`, runner.py:470-476) plus the
  transcript handler delegation passes at delegation.py:593-603. Facade
  contract: luna `agent_facade.py:43-65`. Same return and cost rules as
  `ctx.llm`. Transcript: the event mapping of `_EventFeed`
  (delegation.py:447-549; `_map_event` :489-534 duck-types
  `FunctionToolCallEvent`, `FunctionToolResultEvent` (`.result` or
  `.part`), `PartStartEvent` by `type(ev).__name__`) is extracted into a
  base class `_TranscriptFeed` (events list, `_append`, `_map_event`,
  `handle`; no DB); `_EventFeed` keeps `maybe_flush` (:536-549) on top.
  The host attaches `feed.events` to the journal entry as `transcript`.
  Nested-run guard: `_active_run_id` (runner.py:124-126) is set to the
  run id around every effect execution (master §2 Sub-agents), so a
  sub-agent calling a run tool hits `_nested_run_refusal`
  (agent_tools.py:80-101, used at :460 and :2664). A facade `_aborted`
  answer (luna `agent_facade.py:233-252`: `{"_aborted": reason,
  "error": …}` on turn limit/timeout) fails the effect with
  `EffectError(error)` instead of returning it as a result (deviation
  from v1, see Risks 4).
- `ctx.subtask(playbook, inputs, returns=None)`. Host resolves the
  `Playbook` by name (the select at runner.py:1093-1095); not found →
  `EffectError("Subtask playbook '<name>' not found")` (v1 text,
  runner.py:1098). The child row is created by phase 02's run-creation
  path (the copy of runner.py:387-429) with `trigger=f"subtask:{parent
  run id}"`, `parent_run_id=parent.id`, `is_test=parent.is_test` (v1
  at runner.py:1113-1119) — so the v1 `report_to` rule (runner.py:398-406:
  `subtask:` triggers count as chat-invoked) applies unchanged. The child
  runs through the same segment loop inside the parent's task (awaited,
  like v1's blocking `start_run`, runner.py:257-277, pinned by
  `tests/test_async_run.py::test_blocking_start_run_unchanged_for_subtasks`
  :167-176) with `_active_run_id` set to the child id for the child's
  effects and restored to the parent id afterwards. The effect result is
  the child's `run()` return value held in memory; the entry records
  `child_run_id`. Child failure → `EffectError` carrying the child's
  error text and run id. Cycle guard: the loop carries the ancestor
  playbook-name chain (parent chain + own name); a target already in the
  chain fails the effect with `EffectError` whose text reuses the
  `_nested_run_refusal` wording ("would recurse") and names the chain,
  before any child row is written. The static `detect_subtask_cycles`
  (definition.py:240, validation.py:255-263) does not apply to v2 code.
- `ctx.gather(*handles)`. Shim: each `ctx.<effect>(...)` call returns an
  un-awaited effect handle; `gather` assigns `seq` to the handles in
  argument order, replays every seq present in the journal, and exits
  with the list of the missing ones as pending effects in one
  `outputs/effect.json` (phase 02's exit payload, list form). Host: runs
  the pending list concurrently (`asyncio.gather(...,
  return_exceptions=True)`, one task per effect so `_active_run_id` is
  per task), journals each entry as it settles, then re-runs the segment.
  Replay returns results in argument order; if any entry is `failed`, the
  lowest-seq failure is raised after all have settled. Each element
  counts toward `MAX_EFFECTS` (master §2 Execution model). `gather` itself
  writes no journal entry.
- `ctx.approve(show=…)` — P1 in-process form. Host awaits
  `approvals.request(kind="playbook_effect", summary=…, payload={"run_id",
  "seq", "playbook", "version"}, requested_by_plugin="plugin-playbooks",
  risk_level="medium", conversation_id=run.report_to, presentation=…,
  ttl_seconds=<the effect's `_timeout` or None>)` — kind and payload as
  the master fixes for the park form so plugin/07 swaps only `request` →
  `request_nowait`; `approvals` is `self._ctx.approval` (the seam
  agent_tools.py:2095 uses; luna `plugins/context.py:44` + the `approval`
  property at :562-566). `request` suspends until decided or TTL (luna
  `approval/contract.py:95-118`); the run task blocks; the jail is not
  held (the segment already exited). `presentation` reuses the
  `eyebrow/headline/explanation/changes` shape of agent_tools.py:2164-2169
  with `show` rendered as a text change. Decision mapping: `approved` →
  result `{"approved": True, "request_id", "reason", "decided_by"}`;
  `rejected` → entry `failed` with error type `Rejected` (shim raises
  `ctx.Rejected`); an expired card → error type `ApprovalExpired`
  (`ctx.ApprovalExpired`). Expiry detection: `approvals.get(request_id)`
  (contract.py:178; db_impl.py:825-830) reports `status == "expired"`;
  fallback when `get` is absent: decision `rejected` with `reason ==
  "ttl elapsed"` and `decided_by == "system"`, which is what the TTL
  sweeper stamps and hands to the waiter (db_impl.py `_sweep_once`
  :953-981 via `_row_to_decision` :59-70). Both are catchable
  (`ctx.EffectError` subclasses).
- `failed_handled`: the shim's exit payload carries `handled: [seq, …]`
  — every replayed failure it raised that the code proceeded past (next
  effect awaited or `run()` returned). The host re-stamps those entries
  `failed` → `failed_handled`. An uncaught failure propagates out of
  `run()`, the segment exits with an error and the run fails as phase 02
  defines; the entry stays `failed`.
- `send_chat_message` rule for `ctx.tool` (scope item; verify phase 02
  already has it, else add): copy runner.py:856-867 — no
  `conversation_id` in args → inject `str(run.report_to)` when set; else
  when not `is_test` fail with the v1 text "this run has no chat to report
  to — scheduled/background runs no longer deliver to the ops chat…";
  test runs fall back to the ops conversation as `start_run` computes
  `report_to` (runner.py:398-402).
- Journal entry fields added (in-memory; phase 06 persists them):
  `cost_cents`, `transcript`, `child_run_id`, and the `handled` re-stamp.
- `docs/v2.md`: one subsection per effect (signature, return rule,
  exceptions raised, journal fields).

## Not in this phase

- Park form of `ctx.approve` (`request_nowait`, status `parked`,
  `approval.decided` subscription, resume across restart) — plugin/07;
  the luna one-line kind check in `_on_approval_orphan_decided` — luna/02.
- Persisted `result` column on `playbook_runs` and the additive 14th
  `playbook.run.completed` key; `failed_handled` exclusion from
  `playbook_status` hoisting (agent_tools.py:641-642 today; master cites
  :625-642, the hoist moved to :641-642) — plugin/08.
- Dry-run stubs for llm/agent/subtask/approve — plugin/05.
- Durable journal tables and the columns above — plugin/06.
- `ctx.wait_event` (P2), `ctx.sleep` (deferred): the checker keeps
  rejecting both (plugin/01).
- Trust-model routing (policy/approval gate on tools) — out of scope per
  master §2 Effect execution semantics.
- The v1 runner is untouched: `wait_for_approval` keeps auto-approving
  (runner.py:1060-1069), `_run_subtask` keeps its dict result
  (runner.py:1120-1129).
- UI, skill text, manifest, version bump (see Risks 8).

## Steps

1. Read phase 02's `execution_summary.md` and the final names it settled
   (`JournalStore` entry fields, exit payload shape, effect dispatch
   table, exception family, run-creation helper). Rewrite the symbol
   names below to match before coding. Done when: every symbol this plan
   names exists at HEAD or is listed under "Deviations" in the summary.
2. Shim (`v2/shim.py`): effect handles for `llm`, `agent`, `subtask`,
   `approve` with the argument contracts above (all arguments
   JSON-serialisable; `output=` is v1's schema dict); `ctx.gather`;
   replay raising `ctx.Rejected` / `ctx.ApprovalExpired` from error types;
   `handled` list in the exit payload. Done when: the shim harness phase
   02 tests with (its `tests/test_v2_shim.py` style) shows a gather of
   two un-journaled handles exits with two pending effects with `seq`
   1 and 2 in argument order, and a journal holding both returns
   `[r1, r2]`.
3. `delegation.py`: extract `_TranscriptFeed` (pure mapping) and make
   `_EventFeed(_TranscriptFeed)` keep the DB flush. Done when:
   `pytest tests/test_delegation.py` is green unchanged (no flush test
   exists there today; `maybe_flush`, delegation.py:536-549, is moved
   verbatim, not rewritten) and `_LIVE_FEEDS` (delegation.py:643) still
   holds `_EventFeed` instances.
4. Host (`v2/loop.py`): `_effect_llm`, `_effect_agent`,
   `_effect_subtask`, `_effect_approve` registered in the dispatch table;
   `_playbook_origin_scope` around llm/agent/subtask execution;
   `_active_run_id` set/reset around every effect (verify phase 02 did it
   for `ctx.tool`; keep one place); cost and transcript on the entry.
   Done when: exit tests 1-5 pass.
5. Gather batch executor: multi-element pending exits run through
   `asyncio.gather(return_exceptions=True)`, one task each, each entry
   journaled on settlement. Done when: exit tests 6-7 pass.
6. Subtask: run-row creation via phase 02's helper, child loop awaited
   in-process, `_active_run_id` child/parent swap (ContextVar token
   reset), ancestor chain + cycle guard, child failure → `EffectError`.
   Done when: exit tests 8-11 pass and `active_run_id()` inside the
   parent's next effect equals the parent id.
7. Approve: request construction, blocking await, decision mapping,
   expiry detection (`get` first, reason/decided_by fallback). Done when:
   exit tests 12-14 pass.
8. `failed_handled` re-stamp from the `handled` list. Done when: exit
   tests 7, 10, 13 and 15 show the status.
9. `send_chat_message` rule (add only if phase 02 lacks it). Done when:
   exit test 16 passes.
10. `docs/v2.md` effect subsections. Done when: each of the five effects
    lists its exceptions and journal fields, and the doc states that a
    caught effect failure is journaled `failed_handled`.
11. Full suite from the repo root: `pytest -q` (asyncio_mode=auto,
    pyproject.toml:13-14). Done when: the 403 pre-existing tests are
    green, `tests/test_v2_effects.py` is green, and the red repro pins
    are exactly those listed under Exit tests.
12. Commit on `v2-runtime` locally (no push). Fill in the Execution
    summary below, including "no version bump — batched into the next
    manifest/UI bump" (Risks 8).

## Exit tests

File: `tests/test_v2_effects.py`. Harness: per-test
`create_async_engine("sqlite+aiosqlite://")` + `Base.metadata.create_all`
as in `tests/test_repro_fixplaybooks_runtime.py:57-62`; `_Bus`, `_Tool`,
`_Tools` copied from that file (:29-48) with `fast`/`slow` (gated) tools
(:64-77); v2 playbooks saved through phase 02's helper (a `Playbook` row
whose live version holds `async def run(ctx, inputs)` code). Fakes named
by the master's "same fakes the v1 suite uses":
- `_Agent` from `tests/test_manifest_flow.py:37-49` (`run_llm(prompt,
  **kw)` records `(prompt, kw)`, raises `exc`, returns `(result,
  {"total_tokens": 1})`) — the `run_llm` fake.
- `FakeAgent` from `tests/test_delegation.py:84-110` (`run_turn(prompt,
  **kwargs)` records kwargs, plays `events` through
  `kwargs["event_stream_handler"]`, waits on `gate`, raises `raise_exc`,
  returns `(result, {"total_tokens": 1234})`) with the event fakes at
  :37-80 (`FunctionToolCallEvent`, `FunctionToolResultEvent`,
  `_FunctionToolResultEventV2`, `PartStartEvent`) — the `run_turn` fake.
  Import them from the test module (tests/ is importable: the lifecycle
  file imports `evidence` and `readstage`); copy verbatim only if the
  import proves fragile, and say so in the summary.
- `_Decision` and `_Approvals` from
  `tests/test_repro_fixplaybooks_lifecycle.py:47-73`, extended locally as
  `_GatedApprovals`: `request(**kw)` appends `kw`, awaits an
  `asyncio.Event`, returns the configured `_Decision` (with
  `decided_by`); `get(request_id)` returns an object with `.status`.
  `_Ctx` from :79-87 (`.approval`, `ops_conversation_id()`) is the
  context handed to the loop.
- Usage with cost: `types.SimpleNamespace(cost_cents=3)` returned as the
  second tuple element.

Tests and assertions:
1. `test_llm_returns_dict_with_output_schema`: `_Agent(result={"x": 1})`;
   code returns `await ctx.llm("p", output={"type": "object"})`; run
   `done`, return value `{"x": 1}`; `agent.calls[0][1]["output_schema"]
   == {"type": "object"}` and `["purpose"] == "summarization"`.
2. `test_llm_returns_str_without_output`: `_Agent(result="hi")` → return
   value `"hi"`; the journal entry has `kind == "llm"`, `status == "done"`.
3. `test_llm_records_cost_and_billing_scope`: usage
   `SimpleNamespace(cost_cents=3)` → entry `cost_cents == 3`;
   `monkeypatch` `plugin_playbooks.v2.loop._playbook_origin_scope` with a
   recorder → called once with the playbook. Missing agent (`agent=None`)
   → run `failed`, error contains "requires an injected agent".
4. `test_agent_transcript_on_entry_and_nested_guard`: `FakeAgent` with
   script `[FunctionToolCallEvent("t", "c1"), FunctionToolResultEvent("t",
   "c1", "done"), PartStartEvent("thinking")]`, subclassed so `run_turn`
   also records `active_run_id()` and `json.loads(_nested_run_refusal())`;
   assert the recorded id `== str(run.id)`, the refusal has `"gate" ==
   "nested_playbook_run"`, the entry's `transcript` is the list the same
   script yields in `tests/test_delegation.py` (tool event with `ok is
   True`, then the thought), `calls[0]["tools"] == ["t"]`,
   `["memory_write"] is False`, `["conversation_id"] == run.report_to`.
5. `test_agent_aborted_answer_fails_loud`: `FakeAgent(result={"_aborted":
   "timeout", "error": "turn limit"})` → entry `failed`, run `failed`,
   error contains "turn limit".
6. `test_gather_orders_results_and_runs_concurrently`: code `a, b = await
   ctx.gather(ctx.tool("slow"), ctx.tool("fast")); return [a, b]`; wait
   until `"fast" in calls and "slow-started" in calls` (concurrency),
   `gate.set()`, `wait_for_run`; return value `[slow_result,
   fast_result]`; entries seq 1 = slow, seq 2 = fast, both `done`.
7. `test_gather_raises_first_failure_after_all_settle`: tools `bad1`
   (raises), `slow` (gated), `bad2` (raises); code catches
   `ctx.ToolError as e` and returns `str(e)`; release the gate only after
   both `bad` tools ran; return value names `bad1`; `"slow-finished" in
   calls`; entries: bad1 `failed_handled`, slow `done`, bad2
   `failed_handled`; run `done`.
8. `test_subtask_returns_child_value_and_links_rows`: child code returns
   `{"n": inputs["n"] * 2}`; parent `return await ctx.subtask("child",
   {"n": 2})` → `{"n": 4}`; child `PlaybookRun`: `parent_run_id ==
   parent.id`, `trigger == f"subtask:{parent.id}"`, `is_test ==
   parent.is_test`, `status == "done"`; parent entry `kind == "subtask"`,
   `child_run_id == child.id`; both rows `done`.
9. `test_subtask_cycle_guard_trips_with_existing_refusal`: `a` subtasks
   `b`, `b` subtasks `a`; the `b → a` effect fails with `ctx.EffectError`
   whose text contains "would recurse" and both names; no third run row;
   uncaught → runs `a` and `b` both `failed`.
10. `test_subtask_child_failure_is_catchable`: child raises
    `ValueError("boom")`; parent `except ctx.EffectError as e: return
    str(e)` → run `done`, return value contains "boom" and the child run
    id; entry `failed_handled`; child row `failed`.
11. `test_subtask_unknown_playbook_uses_v1_message`: error text
    `"Subtask playbook 'nope' not found"`, entry `failed`, run `failed`.
12. `test_approve_blocks_until_decided` (v2 twin of
    `tests/test_repro_fixplaybooks_runtime.py::test_wait_for_approval_actually_gates`
    :125-144): code `await ctx.approve(show={"summary": "x"}); await
    ctx.tool("fast")`; start in the background; wait until
    `len(approvals.requests) == 1`; after 0.2 s assert `"fast" not in
    calls` and the run row `status == "running"`; the request kw has
    `kind == "playbook_effect"`, `requested_by_plugin ==
    "plugin-playbooks"`, `payload["run_id"] == str(run.id)`,
    `payload["seq"] == 1`, `presentation["changes"]` non-empty; decide
    `approved` → run `done`, `"fast" in calls`, effect result
    `{"approved": True, ...}`.
13. `test_approve_rejected_raises_ctx_rejected_catchable`: decision
    `_Decision("rejected", reason="no")`; code `except ctx.Rejected as e:
    return f"rejected: {e}"` → run `done`, return value contains "no",
    entry `failed_handled` with error type `Rejected`. Uncaught variant:
    run `failed`, error type `Rejected`.
14. `test_approve_expired_raises_ctx_approval_expired`: `_Decision(
    "rejected", reason="ttl elapsed", decided_by="system")` and `get()`
    → `status == "expired"`; `except ctx.ApprovalExpired` catchable → run
    `done`; a second variant with no `get` attribute takes the
    reason/decided_by fallback and still raises `ctx.ApprovalExpired`.
15. `test_handled_tool_failure_is_failed_handled_and_run_completes`:
    tool raises; code catches `ctx.ToolError` and continues to
    `ctx.tool("fast")`; entry 1 `failed_handled`, entry 2 `done`, run
    `done`.
16. `test_send_chat_message_conversation_rules_match_v1`: run with
    `report_to` set → the handler receives `conversation_id ==
    str(report_to)`; run with `report_to=None`, `is_test=False` → effect
    `failed`, error contains "this run has no chat to report to".

Existing suite: `pytest -q` from the repo root — the 403 pre-existing
tests stay green; `tests/test_manifest_drift.py` green with the three
stamps unchanged at 0.46.0 (pyproject.toml:3, luna-plugin.toml:2,
`plugin_playbooks/__init__.py:624`).

Repro tests: this phase flips
`test_repro_fixplaybooks_runtime.py::test_wait_for_approval_actually_gates`
on the v2 side only — test 12 above carries its identical assertions
(`"fast" not in calls`, status not `done`) against a v2 playbook. The
original stays red as a v1 pin (it drives a v1 `wait_for_approval` step,
runner.py:1060-1069) until the v1 runner is retired, per the repo plan's
Risks 4 flip rule; plugin/07 completes the item with the park form. The
other six pins (`interrupted_run_survives_restart`,
`wait_for_event_actually_waits`, `tool_step_timeout_is_enforced` →
plugin/02, and the three lifecycle pins) are unchanged by this phase: 7
red before, 7 red after.

## Cross-repo checks

- plugin/07 (park form): must reuse this phase's `kind="playbook_effect"`
  and payload `{run_id, seq, playbook, version}` unchanged, replacing
  only the `request` call with `request_nowait` plus the
  `approval.decided` subscription (luna `approval/db_impl.py:877-883`
  emits `{id, decision, reason, decided_by}`; `in_memory_impl.py:227/:233`).
  The in-process form needs no luna change: `request()` is the existing
  protocol (contract.py:95-118), and because a waiter exists the orphan
  continuation path is not involved (db_impl.py `_sweep_once` sets
  `needs_resume_injection = row.id not in self._waiters`; verify at
  execution that a decided in-process card spawns no re-issue turn).
- luna/02 (kind check in `_on_approval_orphan_decided`): required by the
  park form only; nothing here depends on it.
- plugin/08: the `result` column replaces this phase's in-memory child
  value as the subtask source of truth; `failed_handled` entries must be
  excluded from the hoist at agent_tools.py:641-642.
- plugin/06: the durable journal table needs columns for `cost_cents`,
  `transcript` (JSON), `child_run_id`, and the `failed_handled` status.
- plugin/05: dry stubs for `llm/agent/subtask` and `approve → approved,
  dry: true` (master §2 Dry run) must match the return rules fixed here.
- dojoP: none.

## Risks and open questions

1. Master cites card expiry at `approval/db_impl.py:125-137`; at HEAD
   those lines are the `ttl_seconds` parameter of `request()` (:125,
   passed at :137). The expiry stamping lives in `_sweep_once` (:953-981):
   `status="expired"`, `decision="rejected"`, `reason="ttl elapsed"`,
   `decided_by="system"`, waiters woken. Assumption: `ApprovalExpired`
   is detected by `get(request_id).status == "expired"` with the
   reason/decided_by fallback; the in-memory core impl never expires
   (`in_memory_impl.py:42-101`), so tests use the fake above.
2. Assumption: `_timeout=` on `ctx.approve` maps to `ttl_seconds` (engine
   expiry → `ApprovalExpired`, card closed) rather than a host-side
   `wait_for` (which would raise `EffectTimeout` and leave the card
   pending). Default TTL is the engine's when `_timeout` is absent.
3. Assumption: the approve result dict, the `presentation` rendering of
   `show`, and `conversation_id=run.report_to` (possibly None for the
   in-process form) are this plan's choices; the master fixes only kind
   and payload. A run cancelled while blocked leaves the card pending
   until TTL (`request()` never exposes the id to the caller); plugin/07
   fixes that with `request_nowait`.
4. Deviation from strict v1 parity: a facade `_aborted` answer fails the
   `ctx.agent` effect instead of being returned as data (v1
   `_run_agent_step` returns it, runner.py:975; delegation checks it,
   delegation.py:605-619). Chosen for fail-loud; revert if the owner
   wants byte parity.
5. `returns=` on `ctx.subtask` is in the master signature but undefined
   for v2 (v1's `_eval_returns`, runner.py:1131-1137, maps step outputs).
   Assumption: `returns=None` hands back the whole return value; a list
   of keys projects a dict result (missing key → `EffectError`). Confirm
   with the owner; cheap to change.
6. Assumption: a subtask child must be a v2 playbook; a v1 (pblang) child
   fails with `EffectError("subtask target '<name>' is not a v2
   playbook")`. Bridging to the v1 runner is not in the master. The child
   runs its `live_version`, also for test-run parents (v1 parity,
   runner.py:1113-1119).
7. "Cycle guard reusing `_nested_run_refusal`" is read as: the sub-agent
   nested-run guard reuses `_nested_run_refusal` verbatim via
   `_active_run_id`; the subtask cycle guard is an ancestor-chain check
   whose message mirrors its "would recurse" wording. No depth limit
   beyond the chain check and `MAX_EFFECTS`.
8. No manifest or UI change → no version bump; the phase batches into
   the next bump and says so in its summary (repo plan Conventions).
9. `failed_handled` mechanism (master leaves it open): the shim reports
   `handled` seqs on its next exit; for a gather whose raised failure was
   caught, all failed entries of that batch are re-stamped (they were
   settled and the code proceeded past them). Un-awaited handles are
   never executed (as un-awaited coroutines); a checker warning is a
   later phase.
10. Phase 02 names (`JournalStore` fields, exit payload list form,
    dispatch table, `tests/test_v2_shim.py` harness, run-creation helper)
    are taken from the repo plan's layout and may differ; step 1 reconciles
    them. The `send_chat_message` rule may already exist in phase 02 —
    then step 9 only pins it.
11. Cost lands on the journal entry, not on a `PlaybookStepRun` row (v2
    writes none); `_record_step_cost`'s extraction rule is copied, its
    UPDATE target is not. Cost totals surfaced in results are plugin/08+.

## Execution summary

Ran:
Results:
Deviations from this plan:
Learned:
Revised:
