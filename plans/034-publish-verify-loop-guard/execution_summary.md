# 034 — execution summary

Status of this plan: done (2026-09-09; code landed on `v2-runtime` as
**`5260a3f`** "0.57.0: 034 — publish returns verified live_version; loop guard
on re-gated re-issue; honest awaiting hint", on top of 033's `431de46`; the
repro pins `tests/test_repro_fixplaybooks_lifecycle.py::test_publish_success_carries_verified_readback`
and `::test_approved_then_regated_same_payload_trips_loop_guard` are green;
NOT published, NOT pushed — Ship is a later part of the P4 workflow).

## Ran

Ran: 2026-09-09, Claude (Fable 5.1) as the luna-fixer P4 plugin executor.
Entry HEAD for this commit `431de46` (0.57.0 stamps already in place).

- (1) Verified success — `plugin_playbooks/agent_tools.py::_do_publish`:
  after the flip commits, `publish_guard.read_back_live_version(session_factory,
  name)` (`:3805`; a fresh session, never the mutated object) must equal the
  intended `new_live`. Match → the result gains `"published": true`,
  `"live_version": <read-back>`, `"verified": true` and `"hint": "Report
  exactly live_version=N; do not claim any other version is live."`
  (`:3873-3884`). Mismatch → `{"error": "publish reported success but
  live_version reads X (expected N) — do not tell the owner it is live",
  "verified": false, "intended_live_version", "stored_live_version", "hint"}`
  with **no `status`/`published` field**, returned BEFORE `playbook.saved`,
  the UI patch and `announce_publish` — nothing is announced. The
  `playbook_publish` ToolDef description says the same.
- (2) Loop guard — new module `plugin_playbooks/publish_guard.py` (252
  lines, relative imports): `REISSUE_WINDOW = 30 min` (operator decision
  **P4-6**), `remember_card` (`:88`), `note_decision` (`:107`), `clear_card`
  (`:126`), `check_reissue` (`:180`), `broken_flow_result` (`:235`),
  `BROKEN_FLOW_ERROR` = "the approval system approved this but the re-issued
  call re-gated — the approval flow is broken; stop and tell the owner".
  - Storage: six nullable columns on the `playbooks` row
    (`plugin_playbooks/models.py:94-103`: `last_card_action VARCHAR(16)`,
    `last_card_version INTEGER`, `last_card_approval_id VARCHAR(64)`,
    `last_card_raised_at TIMESTAMP`, `last_card_decision VARCHAR(16)`,
    `last_card_decided_at TIMESTAMP`) added on existing installs through
    `_COLUMN_MIGRATIONS` (`plugin_playbooks/__init__.py:58-63`, applied by
    `_ensure_columns` at on_load). No new table → `luna-plugin.toml`
    `db_tables` unchanged.
  - Wiring: `_request_publish_decision` (`agent_tools.py:3382`) calls
    `check_reissue` BEFORE `request_nowait`/`request` (`:3522`); a pending
    decision calls `remember_card` (`:3585`); a verified publish calls
    `clear_card` (`:3830`). `PlaybooksPlugin._start_publish_guard`
    (`__init__.py:846`, called from on_load next to `park.start()` `:936`)
    subscribes `approval.decided` → `note_decision(approval_id, decision)`;
    unsubscribed in `on_unload` (`:1316`).
  - Verdict logic (`check_reissue`): a remembered card for the same
    (playbook, action, version) whose anchor (`decided_at`, else `raised_at`)
    is within the window → engine `approvals.get(id).status` when the engine
    has `get()`, else the recorded decision: `pending` → the awaiting result
    again with the SAME approval_id, no card; `rejected|expired|cancelled|
    superseded` → guard cleared, normal flow; anything else (approved, or an
    engine that cannot say) → `approvals.grants.lookup_detail_full(
    "playbook_change", "", payload, plugin=None)` without minting; a hit
    proceeds (the engine auto-approves inline, audited); no hit or no grants
    API → `{"status": "approval_flow_broken", "error": BROKEN_FLOW_ERROR,
    "playbook", "action", "version", "approval_id", "hint": "Do NOT retry …"}`
    and no `request_nowait` call. A different version is never blocked; the
    guard never raises into a publish (every call wrapped; failure → normal
    flow, logged).
- (3) Awaiting hint (`agent_tools.py::_awaiting`, `:3491`): keeps "WOKEN" and
  "do NOT retry", drops "it is pre-approved and will execute"; now: re-issue
  ONCE, it executes only if the owner's approval matched; a second
  'awaiting' or 'approval_flow_broken' is a platform fault — stop and tell
  the owner; never say a version is live until a publish result says
  `verified=true`.
- Tests: new `tests/test_publish_verify_loop_guard.py` (15: verified success;
  mocked read-back mismatch → error, nothing announced; first-time awaiting
  unchanged + columns written; approved-then-regated on a BLIND engine → guard
  error, one card; recorded `approval.decided` + no grant → guard error, one
  card, grant lookup with the exact payload; grant hit → the woken re-issue
  publishes verified and the memory is cleared; still-pending re-issue →
  awaiting again, same id, no card; rejected → cleared, new card; window
  expiry → fresh card; different version → fresh card; `note_decision`
  ignores unknown ids; the six columns are added to a pre-0.57 schema and
  round-trip; on_load subscription records the decision and unsubscribes on
  unload). `tests/test_approval_wake_on_decision.py` +4 (verified read-back,
  forced mismatch, honest hint, approved-then-regated fails loud).
  `tests/test_v2_end_to_end.py` publish-result key list restated to the new
  contract (`published`/`verified`/`hint` added, nothing removed).
- Also in this commit: the 033 skill-body sentence trimmed back under the
  12 KiB budget (`__init__.py` ~`:452-457`; "THAT is a crawl." dropped).

Commands (repo root): `.venv/bin/python -m pytest -p no:cacheprovider -q
tests/test_loader_style_import.py tests/test_publish_verify_loop_guard.py
tests/test_repro_fixplaybooks_lifecycle.py tests/test_approval_wake_on_decision.py
tests/test_manifest_drift.py tests/test_manifest_set_candidate.py` → all
green before the commit. Full suite after the commit
(`.venv/bin/python -m pytest -p no:cacheprovider -q -rfEsx tests`):
`4 failed, 757 passed, 5 warnings in 69.38s` (baseline at 5714e5d: 7 failed,
731 passed). Red, all intended-red pins for later plans:
`tests/test_repro_fixplaybooks_runtime.py::test_interrupted_run_survives_restart_instead_of_failing`,
`::test_wait_for_approval_actually_gates`, `::test_wait_for_event_actually_waits`,
`::test_tool_step_timeout_is_enforced`.

## Results (against the plan's test list)

- success carries verified live_version — green (`test_success_carries_read_back_live_version`,
  `test_inline_approval_result_is_verified_read_back`).
- forced mismatch → error result — green (`test_read_back_mismatch_is_an_error_and_announces_nothing`,
  `test_forced_read_back_mismatch_is_not_a_success`).
- approved-then-regated → guard error, no second card — green (repro pin +
  `test_approved_then_regated_trips_guard_without_a_second_card`,
  `test_recorded_approval_then_regated_trips_guard`).
- first-time awaiting unchanged — green; guard expires after the window —
  green; guard does not block a different version — green; still-pending
  re-issue → awaiting, no card — green; grant hit lets the re-issue through —
  green.

## Deviations

- Blind engines (no `get()`, no `grants` — the repro pin's stub, old cores):
  a same-payload re-issue inside the window is treated as broken (fail loud),
  because nothing can prove the card is still open. On luna ≥ 0.92.046 both
  APIs exist and the still-pending case returns awaiting. Documented in the
  module docstring.
- Baseline change: 3 of the 7 intended-red pins are this round's contract and
  are now green; the 4 `tests/test_repro_fixplaybooks_runtime.py` pins stay
  red.

## Learned

- `Playbook.approval_id` (UUID) already existed but is the CREATE-time card;
  the guard needs its own string id + action/version/timestamps, hence the
  `last_card_*` columns instead of reusing it.
- SQLite returns naive datetimes for `DateTime(timezone=True)`; the guard
  normalises with `_aware()` before comparing against `datetime.now(UTC)`.
