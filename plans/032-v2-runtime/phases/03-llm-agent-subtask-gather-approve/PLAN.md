# 032 — Phase 03: Effects: ctx.llm, ctx.agent, ctx.subtask, ctx.gather, ctx.approve (in-process form)
Status: pending
Master: /Users/roy/Documents/my-projects-docs/luna-fixer/plans/2026-09-06-fix-playbooks/PLAN.md — §2 Language (effect contracts, `Rejected`/`ApprovalExpired`, `failed_handled` + its hoisting exclusion), §2 Execution model, §2 Effect execution semantics, §2 Sub-agents, §2 Lifecycle (`result` column), §3 P1; master phase M1
Repo / branch: luna-plugins/plugins/plugin-playbooks, branch `v2-runtime` (HEAD 8c31a60 at writing; origin/main 749f126; version 0.46.0 — 0.47.0 since phase 00's 18b9ebe; phase 01 landed as 0f61ba6 with `plugin_playbooks/v2/__init__.py` (`CTX_EXCEPTIONS` incl. `SubtaskFailed`, `APPROVE_RESULT_KEYS`) and `v2/checker.py`). Read-only reference: luna branch `fix-playbooks` @ f05bdf2 (approval engine, agent facade). Commits stay local on `v2-runtime`; nothing is pushed or published.
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
(phase 02's `SegmentLoop`; if phase 02's summary split effect dispatch into
its own module, the handlers go there). The shim side is `v2/shim.py`.
Phase 02's `SegmentLoop(session_factory, tools, events, ctx, journal, …)`
carries no agent facade and no way to start a run, so this phase adds two
constructor arguments: `agent=` (the facade `PlaybookRunner` already holds
as `self._agent`, injected at `__init__.py:751` `agent=ctx.agent`) and
`start_run=` (the bound `PlaybookRunner.start_run`, runner.py:257-277, for
`ctx.subtask`); `PlaybookRunner.__init__` passes both. Below, `self._agent`
/ `self._ctx` mean the loop's copies of the runner's `_agent` / `_ctx`.

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
  truthy `cost_cents` attribute, :1389). Billing scope: the call runs inside
  `_playbook_origin_scope(playbook)` (runner.py:173-189), as v1's
  `_drive_run` does at runner.py:489-490; phase 02's v2 branch sits before
  that `with` block (:484-488), so the loop imports the name into
  `v2/loop.py` (`from ..runner import _playbook_origin_scope`) and calls it
  through its own module global — which is what exit test 3 monkeypatches.
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
  runner.py:1098). Phase 02 defines no run-creation helper (its
  `SegmentLoop.drive(run, …)` receives an existing row), so the child goes
  through the injected `start_run` — v1's own subtask path
  (runner.py:1113-1119): `await start_run(target, inputs=inputs,
  trigger=f"subtask:{parent.id}", parent_run_id=parent.id,
  is_test=parent.is_test)`. `start_run` (:257-277, blocking, pinned by
  `tests/test_async_run.py::test_blocking_start_run_unchanged_for_subtasks`
  :167-176) creates the row via `_create_run` (:373-437) — so the v1
  `report_to` rule (:398-406: `subtask:` triggers count as chat-invoked)
  applies unchanged — and `_drive_run` dispatches a python-format child to
  the same segment loop (phase 02's `sniff_format` branch), awaited inside
  the parent's task. `_drive_run` already sets `_active_run_id` to the
  child id (:478) and resets it to the parent's on exit (:535), so the
  child's effects see the child id and the parent's next effect the parent
  id with no extra code in the loop. The effect result is
  the child's `run()` return value held in memory: `start_run` returns the
  `PlaybookRun` row, not the value, so `SegmentLoop.drive` parks each
  finished run's `LoopResult.value` in a per-instance dict keyed by run id
  and the parent pops it after `start_run` returns (one `SegmentLoop` per
  `PlaybookRunner`, so parent and child share the dict); the entry records
  `child_run_id`. Child failure (row `failed`, no value) → `ctx.SubtaskFailed`
  (phase 01: the `EffectError` subclass in `CTX_EXCEPTIONS`, docs/v2.md §2/§4 — so `except ctx.EffectError` still catches it)
  carrying the child's `run.error` text and run id. Cycle guard: the loop
  carries the ancestor playbook-name chain per run id (parent chain + own
  name, handed to the child before `start_run`); a target already in the
  chain fails the effect with `EffectError` whose text reuses the
  `_nested_run_refusal` wording ("would recurse") and names the chain,
  before any child row is written. The static `detect_subtask_cycles`
  (definition.py:240, validation.py:255-263) does not apply to v2 code.
- `_timeout` enforcement for the three new awaited kinds (master §2
  Language: "`_timeout` is ENFORCED for all effect kinds"; §3 P1 exit
  tests: "`_timeout` enforced per effect"). Phase 02 wraps only the `tool`
  handler in `asyncio.wait_for` and tests only a tool
  (`test_effect_timeout_enforced`), so this phase extends the rule:
  `_effect_llm` and `_effect_agent` run the facade call under
  `asyncio.wait_for(…, timeout=_timeout or DEFAULT_TIMEOUTS[kind])`
  (plugin/01 `plugin_playbooks.v2.DEFAULT_TIMEOUTS`: llm 300, agent 900,
  subtask None = unbounded until plugin/07's `max_duration`);
  `asyncio.TimeoutError` → entry `failed` with `error_type ==
  "EffectTimeout"` (the shim raises `ctx.EffectTimeout` on replay, phase
  02's failed-entry rule), the inner facade task cancelled.
  `_effect_subtask` enforces `_timeout` only when the author gives one
  (default None): the awaited `start_run` runs in its own task under the
  same deadline; on expiry the host cancels that task, which reaches
  `_drive_run`'s `except asyncio.CancelledError` (runner.py:504-510) and
  marks the child row `cancelled` — that handler SWALLOWS the
  cancellation, so the cancelled `start_run` returns its row normally and
  `asyncio.wait_for` alone would not raise (see Risks 12); the host
  therefore records its own deadline-expired flag before cancelling and
  raises `EffectTimeout` from it, never from the child's status. No
  `cancel_run` call: blocking `start_run` registers nothing in
  `PlaybookRunner._tasks` (only `start_run_background` does,
  runner.py:279-300), so `cancel_run` (:614-631) would only take the DB
  fallback. `ctx.approve` is the exception: its `_timeout` maps to
  `ttl_seconds` (below, Risks 2), not to a host-side `wait_for`.
- `ctx.gather(*handles)`. Shim: each `ctx.<effect>(...)` call returns an
  un-awaited effect handle; `gather` assigns `seq` to the handles in
  argument order, replays every seq present in the journal, and exits
  with the list of the missing ones as pending effects in one
  `outputs/result.json` of `kind: "gather"` (phase 02 Risks 1: one
  `result.json` with `kind: effect | gather | return | error`; the
  `gather` kind carries `effects: [{seq, id, effect_kind, args}, …]`
  in argument order). Host: runs
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
- `send_chat_message` rule for `ctx.tool` (assigned to this phase; phase
  02's plan does not carry it — its tool path is vault resolution →
  `rt.handler(**args)` only): copy runner.py:856-867 into the `tool`
  handler — no `conversation_id` in args → inject `str(run.report_to)`
  when set (v1 reads `ctx.conversation_id`, which is `run.report_to`,
  :474) and journal the injected args as the entry's `args`; else when
  not `run.is_test` fail the effect (`failed`, `ToolError`) with the v1
  text "this run has no chat to report to — scheduled/background runs no
  longer deliver to the ops chat…" (:861-867); test runs need no fallback
  here because `_create_run` already stamps `report_to` = origin chat or
  the ops conversation for `is_test` rows (runner.py:401-402).
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
   (`JournalStore` entry fields, the `result.json` kinds, effect dispatch
   table, exception family, `SegmentLoop` constructor). Rewrite the symbol
   names below to match before coding. Done when: every symbol this plan
   names exists at HEAD or is listed under "Deviations" in the summary.
2. Shim (`v2/shim.py`): effect handles for `llm`, `agent`, `subtask`,
   `approve` with the argument contracts above (all arguments
   JSON-serialisable; `output=` is v1's schema dict); `ctx.gather`;
   replay raising `ctx.Rejected` / `ctx.ApprovalExpired` from error types;
   `handled` list in the exit payload. Done when: the shim harness phase
   02 tests with (`tests/_jail.py::real_code_run`, the `[real_jail]`
   tests of `tests/test_v2_loop.py`; phase 02 has no separate shim test
   file) shows a gather of two un-journaled handles exits with one
   `kind: "gather"` payload holding two pending effects with `seq` 1 and
   2 in argument order, and a journal holding both returns `[r1, r2]`.
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
   for `ctx.tool`; keep one place); cost and transcript on the entry;
   `asyncio.wait_for(…, _timeout or DEFAULT_TIMEOUTS[kind])` around the
   llm and agent facade calls. Done when: exit tests 1-5 pass and the
   `llm`/`agent` cases of exit test 17 pass.
5. Gather batch executor: multi-element pending exits run through
   `asyncio.gather(return_exceptions=True)`, one task each, each entry
   journaled on settlement. Done when: exit tests 6-7 pass.
6. Subtask: `start_run=` injected into `SegmentLoop`, child run through
   `start_run` → `_create_run` → `_drive_run`'s v2 branch (awaited
   in-process), the per-run value dict, ancestor chain + cycle guard,
   child failure → `ctx.SubtaskFailed` (phase 01); `_timeout` (when given) as a host-side
   deadline over the `start_run` task with the deadline-expired flag
   (Scope, Risks 12). Done when: exit tests 8-11 pass, the `subtask` case
   of exit test 17 passes, and `active_run_id()` inside the parent's next
   effect equals the parent id (proves `_drive_run`'s set/reset at
   runner.py:478/:535 covers the nested call).
7. Approve: request construction, blocking await, decision mapping,
   expiry detection (`get` first, reason/decided_by fallback). Done when:
   exit tests 12-14 pass.
8. `failed_handled` re-stamp from the `handled` list. Done when: exit
   tests 7, 10, 13, 15 and the catchable variant of 17 show the status.
9. `send_chat_message` rule in the `tool` handler (phase 02's plan does
   not carry it; if its summary shows it was added anyway, step 9 only
   pins it). Done when: exit test 16 passes.
10. `docs/v2.md` effect subsections. Done when: each of the five effects
    lists its exceptions and journal fields, and the doc states that a
    caught effect failure is journaled `failed_handled`.
11. Full suite from the repo root: `pytest -q` (asyncio_mode=auto,
    pyproject.toml:13-14). Done when: every pre-existing test that
    plugin/02's summary records as green is still green,
    `tests/test_v2_effects.py` is green, and the red repro pins are
    exactly those listed under Exit tests.
12. Commit on `v2-runtime` locally (no push). Write
    `execution_summary.md` in this folder per the template below,
    including "no version bump — batched into the next manifest/UI bump"
    (Risks 8); revise plugin/05-08 with the final approve request shape
    and entry fields.

## Exit tests

File: `tests/test_v2_effects.py`. Harness: per-test
`create_async_engine("sqlite+aiosqlite://")` + `Base.metadata.create_all`
as in `tests/test_repro_fixplaybooks_runtime.py:57-62`; `_Bus`, `_Tool`,
`_Tools` copied from that file (:29-48) with `fast`/`slow` (gated) tools
(:64-77); v2 playbooks saved through phase 02's helper (a `Playbook` row
whose live version holds `async def run(ctx, inputs)` code). The runner
is built as in that file (:78) plus `agent=` and `context=`; note
`_create_run` reads `self._ctx.current_conversation_id` (runner.py:387)
whenever a context is given, so the local `_Ctx` must carry
`current_conversation_id = None` (the lifecycle file's `_Ctx` does not).
Fakes as the repo `PLAN.md` phase index requires ("the v1 suite's
`run_llm`/`run_turn` fakes"):
- `_Agent` from `tests/test_manifest_flow.py:37-49` (`run_llm(prompt,
  **kw)` records `(prompt, kw)`, raises `exc`, returns `(result,
  {"total_tokens": 1})`) — the `run_llm` fake.
- `FakeAgent` from `tests/test_delegation.py:85-110` (`run_turn(prompt,
  **kwargs)` records kwargs, plays `events` through
  `kwargs["event_stream_handler"]`, waits on `gate`, raises `raise_exc`,
  returns `(result, {"total_tokens": 1234})`) with the event fakes at
  :37-82 (`FunctionToolCallEvent`, `FunctionToolResultEvent`,
  `_FunctionToolResultEventV2`, `PartStartEvent`) — the `run_turn` fake.
  Import them from the test module (`tests/` has no `__init__.py`, so
  pytest's rootdir conftest puts `tests/` on `sys.path`; the lifecycle
  file already imports `evidence` and `readstage` that way); copy
  verbatim only if the import proves fragile, and say so in the summary.
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
    id; entry `failed_handled` with error type `SubtaskFailed` (phase 01:
    the raised class is `ctx.SubtaskFailed`); child row `failed`.
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
    `{"approved": True, ...}` and `set(entry["result"]) ==
    plugin_playbooks.v2.APPROVE_RESULT_KEYS` (phase 01: `{approved,
    request_id, reason, decided_by}` — the shim half of the doc↔runtime
    sync; `tests/test_v2_contract_doc.py::test_doc_approve_result_keys_match_constant`
    is the doc half).
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
16. `test_send_chat_message_conversation_rules_match_v1`: a
    `send_chat_message` fake tool; run with `report_to` set (write
    `report_to` on the row before driving, or use `trigger="agent"` with a
    `_Ctx.current_conversation_id`, runner.py:398-404) → the handler
    receives `conversation_id == str(report_to)` and the journal entry's
    `args` carry it; run with `report_to=None`, `is_test=False` → effect
    `failed`, error contains "this run has no chat to report to", run
    `failed`.
17. `test_effect_timeout_enforced_for_llm_agent_subtask` (extends phase
    02's tool-only `test_effect_timeout_enforced` to the three kinds;
    master §3 P1 "`_timeout` enforced per effect"), parametrized over
    `llm`, `agent`, `subtask`, each with a gated fake and `_timeout=1`:
    `llm` — a local `_Agent` subclass whose `run_llm` awaits an
    `asyncio.Event` that is never set; `agent` — `FakeAgent(gate=
    asyncio.Event())` never set; `subtask` — a child playbook whose code
    awaits `ctx.tool("slow")` on the gated tool, gate never set. Code
    `await ctx.<kind>(...)` with `_timeout=1`; `wait_for_run(timeout=2.5)`
    → run `failed`, the entry `failed` with `error_type ==
    "EffectTimeout"`, `run.error` contains "timed out"; for `subtask` the
    child row is `cancelled` (runner.py:504-510) and the parent entry
    still carries `child_run_id`. Catchable variant, same three kinds:
    code `except ctx.EffectTimeout: return "late"` → run `done`, return
    value `"late"`, entry `failed_handled`. The fake's gate is released
    in teardown so no task outlives the test.

Existing suite: `pytest -q` from the repo root — every pre-existing test
plugin/02's summary records as green stays green (plugin/00 removed
`tests/test_specs.py` and `tests/test_versioned_specs.py` and added
`tests/test_no_spec_feature.py`, so the 8c31a60 figures 403/410 no
longer apply — phase 00's summary records 378 green + 7 red = 385);
`tests/test_manifest_drift.py::test_version_stamps_agree`
green with the three stamps unchanged from plugin/02's summary (0.47.0
if plugin/01-02 did not bump further; pyproject.toml:3, luna-plugin.toml:2,
`plugin_playbooks/__init__.py:616` at 18b9ebe).

Repro tests: this phase flips
`test_repro_fixplaybooks_runtime.py::test_wait_for_approval_actually_gates`
on the v2 side only — test 12 above carries its identical assertions
(`"fast" not in calls`, status not `done`) against a v2 playbook. The
original stays red as a v1 pin (it drives a v1 `wait_for_approval` step,
runner.py:1060-1069) until the v1 runner is retired, per the repo plan's
Risks 4 flip rule; plugin/07 completes the item with the park form. The
other six pins (`interrupted_run_survives_restart` → plugin/06,
`wait_for_event_actually_waits` → plugin/07,
`tool_step_timeout_is_enforced` → plugin/02 (v2 twin only, original
stays red), and the three lifecycle pins → P0 plans) are unchanged by
this phase: 7 red before, 7 red after (the collected total is the count
plugin/02's summary records plus this phase's 17 tests; the 8c31a60
figure of 410 = 403 green + 7 red predates plugin/00's test deletions
and additions — see Existing suite above).

## Cross-repo checks

- plugin/07 (park form): must reuse this phase's `kind="playbook_effect"`
  and payload `{run_id, seq, playbook, version}` unchanged, replacing
  only the `request` call with `request_nowait` plus the
  `approval.decided` subscription (luna `approval/db_impl.py:882-889`
  emits `{id, decision, reason, decided_by}`, logged at :876-881;
  `in_memory_impl.py:232-238`, logged at :226-231).
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
10. Phase 02 names used here (`SegmentLoop`, `MemoryJournalStore` entry
    fields, the `kind: "gather"` payload in `result.json`, the dispatch
    table, `tests/_jail.py::real_code_run`) are taken from phase 02's
    plan, not its summary, and may differ once it has run; step 1
    reconciles them. Phase 02 defines no run-creation helper — this plan
    uses `PlaybookRunner.start_run` — and no `send_chat_message` rule;
    if its summary shows either was added anyway, step 6 / step 9 only
    pin them.
11. Cost lands on the journal entry, not on a `PlaybookStepRun` row (v2
    writes none); `_record_step_cost`'s extraction rule is copied, its
    UPDATE target is not. Cost totals surfaced in results are plugin/08+.
12. Subtask `_timeout` and the swallowed cancel: `_drive_run` catches
    `asyncio.CancelledError` and returns normally after marking the run
    `cancelled` (runner.py:504-510, plans/009 behaviour). Under
    `asyncio.wait_for` (3.11 `_cancel_and_wait` + `fut.result()`; 3.12
    `timeouts.timeout`, which converts only a propagating
    `CancelledError`) a child that swallows the cancel makes `wait_for`
    return the row instead of raising `TimeoutError`, so the host cannot
    rely on the exception. Assumption: the host owns the deadline (set a
    `timed_out` flag, cancel the child task, await it, then raise
    `EffectTimeout` if the flag is set); the child row reads `cancelled`,
    never `failed`. Exit test 17's `subtask` case pins this. The facade
    fakes (`run_llm`/`run_turn`) do not swallow cancellation, so the plain
    `wait_for` rule holds for llm/agent; a real facade that swallowed it
    would need the same flag — check `agent_facade.py` at execution.
    `DEFAULT_TIMEOUTS["subtask"]` is None (plugin/01), so without an
    explicit `_timeout` a subtask is unbounded until plugin/07's
    `max_duration`.

## Execution summary

Written to `execution_summary.md` in this folder after the phase runs
(never created empty up front), using this template:
- Ran: (commands, dates, HEAD before/after; the `pytest -q` totals)
- Results: (every exit test 1-17 with its outcome; the 7 red repro pins
  confirmed unchanged; the three version stamps and the green/red/collected
  totals as recorded; anything red and why)
- Deviations from this plan: (what changed and why — phase 02 name
  reconciliation from step 1, the Risks 2-7 and 12 assumptions confirmed
  or corrected, "no version bump — batched into the next manifest/UI
  bump")
- Learned: (facts that change later phases — the approve request shape
  plugin/07 must keep, the entry fields plugin/06 must persist)
- Revised: (which later phase files were edited because of this, and how)
