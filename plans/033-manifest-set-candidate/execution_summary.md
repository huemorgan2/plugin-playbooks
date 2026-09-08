# 033 — execution summary

Status of this plan: done (2026-09-09; code landed on `v2-runtime` as
**`431de46`** "0.57.0: 033 — playbook_manifest_set saves a candidate, never
flips live"; the repro pin
`tests/test_repro_fixplaybooks_lifecycle.py::test_manifest_set_does_not_flip_live`
is green; NOT published, NOT pushed — the marketplace publish of 0.57.0 and
the managed upgrade on `vaselin-error-log-tracker` are the Ship part of the
P4 workflow, covered by the same owner approval).

## Ran

Ran: 2026-09-09, Claude (Fable 5.1) as the luna-fixer P4 plugin executor, on
plugin-playbooks `v2-runtime`. Entry HEAD `7fa0e8a` (docs; code `5714e5d` =
0.56.0; suite 7 red pins / 731 green; status ` M uv.lock` only — pre-existing,
never staged, never discarded). Local commits only; nothing pushed, nothing
published; no `vaselin-*` agent touched. No CHANGELOG in this repo (none kept).

- Step 1 (mirror): `plans/033-manifest-set-candidate/PLAN.md` (this folder)
  and `plans/034-publish-verify-loop-guard/PLAN.md`, both `Status: approved`
  with the owner's in-session approval text and date, re-anchored at 5714e5d.
- Step 2 (the change), `plugin_playbooks/agent_tools.py`:
  - `_manifest_set` (`:3249`) rewritten on the `playbook_edit` write path:
    lock the row → `candidate_conflict(session, playbook, author)` (a
    foreign author's candidate → `{"saved": false, "error":
    conflict_message(...), "conflict": ...}`, nothing written) → base = the
    pending candidate row when there is one (`_get_version_row`), else the
    live row → `mint_version(definition, code, manifest=<new>, author,
    message="manifest updated[ on candidate][: why]", format)` →
    `playbook.candidate_version = playbook.version`; a never-published
    playbook mirrors the manifest on the row (same as `playbook_edit`).
    **`playbook.live_version` is no longer written here** (the invariant
    scan's `KNOWN_SIDE_DOORS` is empty). Result:
    `{"playbook", "version", "candidate_version", "live_version", "status":
    "manifest_candidate_saved", "manifest_chars", "note": "manifest saved as
    candidate vN — publish to go live", "next": ...}`; emits
    `playbook.candidate.saved` (not `playbook.saved` — nothing live changed).
    Operator decision **P4-5** (pending same-author code candidate → ONE
    merged candidate carrying both the code and the new manifest) is
    documented in the code comment.
  - `_do_publish`: `manifest_after = (row.manifest or "") or manifest_before`
    (the card shows the manifest diff) and the flip is
    `_apply_version_to_live(playbook, row, restore_manifest=True)` for
    candidates too — the candidate row's manifest goes live with its content.
  - ToolDef `playbook_manifest_set` description: "Saves a CANDIDATE (merged
    onto the pending candidate if you have one) — the live playbook is
    unchanged until playbook_publish; nothing goes live from this call."
    Read-stage instruction text updated the same way.
- `plugin_playbooks/routes.py`: promote (`:1278`) applies the candidate row's
  manifest (`restore_manifest=True`); `put_manifest` (owner REST, still a
  direct live write — whitelisted) also stamps `cand.manifest` on a pending
  candidate row (`:1457`) so publishing it later cannot revert the owner's
  edit.
- Wording that promised instant manifest flips: skill body
  `plugin_playbooks/__init__.py` (~`:455`), delegation checklist item 5 +
  brief text (`plugin_playbooks/delegation.py`, both v1/v2 copies),
  `plugin_playbooks/luna-plugin.toml:161` description, `README.md:36`.
- Stamps **0.57.0** ×3 (`pyproject.toml:3`, `plugin_playbooks/luna-plugin.toml:2`,
  `plugin_playbooks/__init__.py:729`) — operator decision P4-1.
- Tests: new `tests/test_manifest_set_candidate.py` (6 cases: live unchanged +
  candidate row; the manifest candidate publishes through
  `_request_publish_decision` with a "Manifest"-only card and applies the
  manifest on the flip; a pending decision flips nothing; pending code
  candidate + manifest_set → one merged candidate that publishes both; a
  foreign candidate is refused; owner REST `PUT …/manifest` stamps the
  pending candidate and a later publish keeps it). Restated to the new
  contract: `tests/test_manifest_flow.py::test_manifest_set_snapshots_and_bumps`
  (was pinning `live_version == 2`) and
  `tests/test_candidate_flow.py::test_manifest_set_with_pending_candidate_keeps_versions_unique`
  (was pinning the live flip over the candidate); `KNOWN_SIDE_DOORS` emptied
  in `tests/test_v2_live_version_invariant.py` as that test demands once the
  fix lands. Tool names / policies / tables unchanged
  (`tests/test_manifest_drift.py` green).

Commands (repo root): `.venv/bin/python -m pytest -p no:cacheprovider -q
tests/test_loader_style_import.py` (2 green) before the commit; the targeted
set `tests/test_loader_style_import.py tests/test_manifest_flow.py
tests/test_candidate_flow.py tests/test_v2_live_version_invariant.py
tests/test_manifest_set_candidate.py tests/test_repro_fixplaybooks_lifecycle.py
tests/test_manifest_drift.py tests/test_delegate_prompt.py
tests/test_version_routes.py` → green except the two 034 pins (fixed in the
next commit). Full-suite line: see plans/034 execution summary (one suite for
both commits): `4 failed, 757 passed, 5 warnings in 69.38s` — red = the four
intended-red runtime pins in `tests/test_repro_fixplaybooks_runtime.py`.

## Results

- Repro pin `test_manifest_set_does_not_flip_live`: green (live stays 1 with a
  candidate v2 pending; no approval requested for a save).
- The side door is gone from the AST scan: `found - WHITELIST == set()`.

## Deviations

- The skill-body sentence added here pushed `_AUTHORING_SKILL_BODY` 112
  bytes over the 12 KiB budget (`tests/test_authoring_ergonomics.py::
  test_payload_diet_budgets`) — noticed on the full suite and trimmed in the
  034 commit (`5260a3f`), same meaning, no test touched.
- Not in the plan: the owner REST `put_manifest` candidate stamp (routes.py
  `:1457`) — needed so a manifest-carrying candidate cannot revert the owner's
  later REST edit on publish.

## Follow-up for the owner

- A manifest-only candidate now needs a green run of that exact version to
  pass the publish gate (unless `publish_require_run` is off) — by design
  ("publish then runs the normal gates"), but it makes a manifest-only change
  one `playbook_run_candidate` more expensive than before.
