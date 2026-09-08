# 034 — publish returns a verified live_version; loop guard on a re-gated re-issue; honest awaiting hint

Status: approved — owner, in session, 2026-09-08, via the master plan luna-fixer plans/2026-09-06-playbook-publish-verify/PLAN.md (Status: approved)

Approval text (master): "all approved go for it and also set production -
image from the branch and see that agents actually work.. you can make new
ones if u like". Ship scope for this round: luna fix-playbooks branch image +
plugin managed upgrade on vaselin-error-log-tracker, plus new vaselin-* test
agents for verification. Fleet-wide promote is a separate approval.

Written 2026-09-09. Mirror of the master plan for this repo; the master stays
the authority. Single phase: this file + `execution_summary.md`.

## Baseline

Same as plans/033 (v2-runtime @ 7fa0e8a, code 5714e5d = 0.56.0, 7 red pins /
731 green). 033 lands first on this branch; 034 is the second commit of
0.57.0.

## Evidence (master)

2026-09-05, vaselin-scanny-2: the agent declared "v59 is live" and later "v61
is live" while `live_version` read 60 both times; the approval wake →
re-issue → re-gate loop minted 3 cards for one publish (core grant hole,
luna plan 108). Tickets b5cbdb1b, fc1a705c; bugs-catalog B1, C, F3-F5.

## Diagnosis re-anchored at 5714e5d

- `plugin_playbooks/agent_tools.py:3490-3748` `_do_publish`: after the flip
  (`_apply_version_to_live` :3679, `new_live = playbook.live_version` :3685,
  commit :3687) the success result :3725-3748 echoes the in-memory
  `new_live`; nothing re-reads the stored row, no `verified` field, and the
  note invites an unconstrained narrative.
- `_request_publish_decision` :3312-3489: payload identity
  `{"name", "version", "action"}` :3416; pending → awaiting JSON :3463-3481
  whose hint promises "re-issue this exact call — it is pre-approved and
  will execute" :3477-3479. No memory of the card just raised: a re-gated
  re-issue mints a fresh card with the same promise, unbounded.
- Plugin state store: no KV; plugin-owned tables are `models.py` models
  created at on_load (`__init__.py:848-851`), late columns via
  `_COLUMN_MIGRATIONS` (`__init__.py:21-58`, `ALTER TABLE … ADD COLUMN` in
  `_ensure_columns` :83-101). `approval.decided` is already subscribed for
  parked runs (`v2/park.py:155-163`, wired at `__init__.py:891-894`).
- Core engine facts used defensively (luna 0.92.046, `luna/approval/db_impl.py`):
  `request_nowait` dedups a still-pending same-payload card (same
  request_id), `get(request_id) -> ApprovalRequest(status)` :825,
  `grants.lookup_detail_full(kind, target, payload, plugin=)` :96 /
  `grants.py:144` — the exact-payload pre-grant a woken re-issue relies on
  (`_ORPHAN_GRANT_TTL_SECONDS = 300`, db_impl.py:39).

## Date validation (master)

plugin-playbooks 0.46.0 (wake/re-issue contract from plans/030) shipped
2026-09-05 ~11:35 with luna 0.92.040; events 14:45-16:24 the same day —
post-rollout, current code; the handler is unchanged at 5714e5d.

## Reproduction

Production transcripts (two false "live" claims, three loop cycles) + the
red pins `tests/test_repro_fixplaybooks_lifecycle.py::test_publish_success_carries_verified_readback`
and `::test_approved_then_regated_same_payload_trips_loop_guard` (blind
approvals stub: the plugin must trip the guard from its OWN state without a
second `request_nowait`).

## Change

1. **Verified success.** After the flip commits, `_do_publish` re-reads the
   playbook row in a fresh session (`publish_guard.read_back_live_version`,
   monkeypatchable = the plan's "mocked store"). Match → the result carries
   `"published": true, "live_version": N, "verified": true` and the hint
   "Report exactly live_version=N; do not claim any other version is live."
   Mismatch → `{"error": "publish reported success but live_version reads X
   — do not tell the owner it is live", ...}` with no `status`/`published`
   field; nothing is announced.
2. **Loop guard.** New module `plugin_playbooks/publish_guard.py`; state on
   the `playbooks` row (columns via `_COLUMN_MIGRATIONS`, 0.57.0):
   `last_card_action VARCHAR(16)`, `last_card_version INTEGER`,
   `last_card_approval_id VARCHAR(64)`, `last_card_raised_at TIMESTAMP`,
   `last_card_decision VARCHAR(16)`, `last_card_decided_at TIMESTAMP`.
   `remember_card` writes the first four when a card is raised (pending);
   `note_decision` fills decision/decided_at from `approval.decided`
   (subscribed in on_load next to park). Before raising a card,
   `_request_publish_decision` calls `check_reissue`: a remembered card for
   the same (playbook, action, version) whose anchor (decided_at, else
   raised_at) is within **30 minutes** (operator decision P4-6) is examined —
   engine `get()` status when available, else the recorded decision:
   still pending → the awaiting result again (same approval_id, no new
   card); rejected/expired → guard cleared, normal flow; approved or
   undeterminable → the exact-payload grant is looked up without minting
   (`grants.lookup_detail_full`); a hit proceeds (the engine auto-approves
   inline, audited), anything else returns the hard error "the approval
   system approved this but the re-issued call re-gated — the approval
   flow is broken; stop and tell the owner" (`status:
   "approval_flow_broken"`, no retry hint) and mints nothing.
3. **Awaiting hint honesty.** The hint keeps WOKEN / do-NOT-retry and now
   says the re-issue executes only if the pre-approval matched; a second
   "awaiting" or the loop-guard error is a platform fault — stop and tell
   the owner.
4. Tests (`tests/test_approval_wake_on_decision.py` + new
   `tests/test_publish_verify_loop_guard.py`): success carries verified
   live_version; forced mismatch (mocked read-back) → error result, no
   success field; approved-then-regated same payload → guard error and no
   second card; first-time awaiting flow unchanged; guard expires after the
   window; guard does not block a different version; still-pending re-issue
   returns awaiting without a new card; a grant hit lets the woken re-issue
   through.

## Version / rollout / risk

- 0.57.0 (shared with 033). New columns are additive and nullable; manifest
  `db_tables` unchanged (no new table).
- Risk low-medium: (1) is read-after-write on the same store; (2) is
  per-playbook+action+version and time-bounded — a genuinely new publish
  (new version) is never blocked; a bug in (2) fails loud, not silent.
