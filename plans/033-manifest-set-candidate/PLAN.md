# 033 — playbook_manifest_set saves a candidate, never flips live

Status: approved — owner, in session, 2026-09-08, via the master plan luna-fixer plans/2026-09-06-manifest-set-live-bypass/PLAN.md (Status: approved)

Approval text (master): "all approved go for it and also set production -
image from the branch and see that agents actually work.. you can make new
ones if u like". Ship scope for this round: luna fix-playbooks branch image +
plugin managed upgrade on vaselin-error-log-tracker, plus new vaselin-* test
agents for verification. Fleet-wide promote is a separate approval.

Written 2026-09-09. Mirror of the master plan for this repo (luna-fixer
CLAUDE.md "Fixing discipline"); the master stays the authority. Single phase:
this file + `execution_summary.md` in the same folder.

## Baseline

- Branch `v2-runtime` @ 7fa0e8a (docs; code 5714e5d = 0.56.0). No remote
  branch; origin/main 749f126 (0.46.0). Working tree ` M uv.lock` only
  (pre-existing, never staged).
- Stamps 0.56.0 ×3: `pyproject.toml:3`, `plugin_playbooks/luna-plugin.toml:2`,
  `plugin_playbooks/__init__.py:728` (`tests/test_manifest_drift.py::test_version_stamps_agree`).
- Suite at 5714e5d: 7 failed (intended-red repro pins), 731 passed.
  Three of the seven pins are THIS plan's and 034's contract
  (`tests/test_repro_fixplaybooks_lifecycle.py::test_manifest_set_does_not_flip_live`,
  `::test_publish_success_carries_verified_readback`,
  `::test_approved_then_regated_same_payload_trips_loop_guard`) — they turn
  green when the fixes land; the four runtime pins stay red.

## Evidence (master)

2026-09-05, vaselin-scanny-2, playbook `candidate-intake`: v60 (manifest-only,
author="agent") went live at 14:44:54 with no publish call, no test-run gate,
no approval card — silently superseding the v59 candidate awaiting owner
approval. The owner approved v59 three times against a live pointer that had
already moved. Tickets b5cbdb1b / fc1a705c; `luna-input/2026-09-05/bugs-catalog.md` B2.

## Diagnosis re-anchored at 5714e5d

- `plugin_playbooks/agent_tools.py:3247-3275` `_manifest_set`: locks the row,
  `_ensure_live_row` (:3258), `playbook.manifest = manifest` (:3260),
  `mint_version(... manifest=manifest ...)` (:3261-3266), then
  **`playbook.live_version = playbook.version` (:3267)** — a live flip with no
  candidate, no gate, no approval. ToolDef :3277-3304 (`policy="auto_approve"`).
- The only legitimate live flip is `_apply_version_to_live` (:1625-1643,
  `:1640`) reached from `_do_publish` (:3679-3681) after the gates and the
  owner card. `tests/test_v2_live_version_invariant.py:53-56` lists
  `_manifest_set` as the one KNOWN side door and insists the entry is dropped
  when this plan lands.
- Candidate path to mirror: `playbook_edit` write stage :3049-3119
  (`candidate_conflict` :3066 → `conflict_message`; `mint_version` :3097;
  `playbook.candidate_version = playbook.version` :3104; never-published
  mirror :3105-3111; `playbook.candidate.saved` :3117).
- Candidate publish today keeps the LIVE manifest: `_do_publish` :3629-3632
  (`manifest_after = manifest_before if is_candidate`) and :3679-3681
  (`restore_manifest=not is_candidate`). A manifest candidate can only go
  live if the candidate row's manifest is applied on publish.

## Date validation (master)

Tool shipped in 0.46.0 (749f126) and is unchanged at 5714e5d (0.56.0, the
managed version on vaselin-error-log-tracker since 2026-09-08). Event
2026-09-05 14:44:54 — current code; design hole, no earlier fix touched it.

## Reproduction

Natural production reproduction (v60 above) + the red pin
`tests/test_repro_fixplaybooks_lifecycle.py::test_manifest_set_does_not_flip_live`
(v1 live, v2 candidate, manifest_set → asserts live stays 1 and no approval
was requested). No throwaway agent needed.

## Change

1. `_manifest_set` saves the manifest as a CANDIDATE through the edit path:
   lock → `candidate_conflict(session, playbook, writer_identity())` (a
   foreign author's candidate → refuse with `conflict_message`, nothing
   saved) → content = the pending candidate row's definition/code/format
   when one exists (operator decision P4-5: MERGE — the manifest is applied
   on top of the single candidate; the previous candidate row stays in
   history), else the live content → `mint_version(... manifest=new ...)` →
   `playbook.candidate_version = playbook.version`. `live_version` is never
   written. A never-published playbook mirrors the manifest onto the row
   (reads show it), as edit mirrors code.
2. Result: `{"status": "manifest_candidate_saved", "version": N,
   "candidate_version": N, "live_version": L, "note": "manifest saved as
   candidate vN — publish to go live", "next": <edit-style hint>}`.
3. Candidate publish applies the candidate row's manifest
   (`restore_manifest=True` for candidates; `manifest_after = row.manifest`
   so the approval card shows the manifest diff). The owner REST
   `PUT /playbooks/{name}/manifest` (routes.py:1434-1459, the UI path, stays
   direct) also stamps the new manifest onto a pending candidate row so a
   later publish cannot revert the owner's edit.
4. Tool description + every prompt/skill/manifest sentence promising an
   instant flip says "saves a candidate — publish to go live".
5. Tests: the pin above; `tests/test_manifest_flow.py` /
   `tests/test_candidate_flow.py` manifest_set tests re-stated for the new
   contract (candidate created, live unchanged, one merged candidate with a
   pending same-author candidate); publish of a manifest candidate goes
   through `_request_publish_decision` (observed via the approvals stub) and
   applies the manifest; foreign-author candidate → refusal; the
   live_version-writer invariant drops the side-door entry.

## Version / rollout / risk

- 0.57.0 (one bump shared with 034; operator decision P4-1), stamps ×3.
- Behaviour change: a manifest edit now needs `playbook_publish` and passes
  its normal gates (a green candidate run unless `publish_require_run` is
  off) — one extra step; that step is the point.
- Risk low-medium; failure mode is a refused save (loud), never a silent
  live change.
