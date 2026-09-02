# Plan 022 — Truthful evidence, fail-closed approvals, coding-agent-grade reads

**Status:** planned
**Version:** 0.39.0
**Driver:** the 2026-09-01/02 meltdown (forensics:
`luna-plugins/research/playbooks-meltdown-2026-09-02/`, esp. `plugin-playbooks-changes.md` and
`platform-root-causes.md`). Core-side fixes ship as luna plan 101; this plan fixes everything
plugin-side.

## Product contract (Roy, 2026-09-02)

- Plans stay removed (021 did this; they were shit). Gatekeeping stays reduced — gates made the
  agent stupid and were routed around anyway (edit_force).
- **Tests are the quality mechanism**: specs required, and minting a new version must carry the
  previous version's specs forward so tests never silently vanish.
- **Coding-agent-grade read access**: the agent must be able to read ALL versions — code,
  specs, manifests, run history — the way a coding agent reads all files. Openness + tests
  beats gates.

## Root causes addressed

- **RC6 — evidence lies.** `publish.py:182-185`: `test_run_gate` with `require=False` returns a
  FAILED run in the evidence slot; `routes.py:1268` hardwires `require=False`;
  `agent_tools.py:1614` and `publish.py:266` hardcode "green" in announcements regardless of
  run status. During the incident, failed runs were presented as green evidence.
- **RC-approvals — fail-open in-handler approvals** (e9f2481:1702-1713): on timeout/error the
  publish proceeded. Combined with the 30s ToolDef timeout (memory:
  in-handler-approvals-need-long-tool-timeouts) this orphaned cards AND published anyway.
- **RC4 — undisconfirmable dry runs.** `runner.py:672` emits `{_dry: true}` stubs without
  checking the tool exists (real check at :690-697 only in the live path). A playbook
  referencing a nonexistent tool dry-runs green, then fails live — feeding the false
  "kind: code invalid" diagnosis.
- **RC5-adjacent — history integrity.** `versioning.py:166` healer keeps the OLDEST duplicate
  row regardless of content (during the incident the oldest happened to be the good one — luck,
  not design). `routes.py:1183` `restore_manifest` promotes a row's manifest even when NULL,
  nulling the live manifest.
- **Gated blindness.** Read tools exist but the agent cannot read old versions'
  code/specs/runs as first-class files; during the incident it diagnosed blind.

## Changes

### P1 — evidence truthfulness

1. `test_run_gate`: a failed run is NEVER returned in the evidence position. Signature becomes
   explicit: `(gate_msg, evidence_run, failed_run)`; with `require=False` a failure means
   `evidence_run=None` and the failure is carried separately for honest reporting.
2. All publish/announce surfaces (`agent_tools.py:1614`, `publish.py:266`, cards, ops-chat
   events) print the REAL run status: `passed` / `failed (kept: publish ungated)` / `no run`.
   The word "green" appears only when the run passed.
3. Publish result payload includes `evidence: {run_id, status, spec_count}` so downstream chats
   and the ops inbox see the truth machine-readably.

### P2 — approvals fail closed

In-handler approval waits: on timeout, error, or card orphaning the operation ABORTS with a
clear message ("approval not obtained — nothing was published"). Keep the long-timeout ToolDef
stamps from 0.30.3. No code path may treat "approval infrastructure failed" as "approved".

### P3 — spec carry-forward on mint

Minting a new version copies the parent version's specs (content-addressed copy, provenance
field `carried_from: v<N>`). New specs add; carried specs stay until explicitly deleted with a
reason. `playbook_spec_delete` on a carried spec requires a reason string — visibility, not a
gate.

### P4 — coding-agent-grade reads (the openness milestone)

New/expanded tools, ALL declared `modes=["planning", "building"]` so they are visible in the
ops chat's identify state (identify inherits planning since core plan 100):

1. `playbook_versions` — list every version of a playbook: version, created_at, has_code,
   has_manifest, spec_count, run counts, published/live markers. No pagination games; it's a
   file listing.
2. `playbook_version_read` — full read of ANY version: code, manifest, specs, recent runs.
   Equivalent of `cat` on an old file.
3. `playbook_version_diff` — unified diff of code+manifest between any two versions.
4. `playbook_runs` gains `version` filter and returns full failure output (not truncated
   summaries) — reading a failing run must be as good as reading a CI log.
5. Existing read tools (`playbook_read`, `playbook_list`, run readers) audited to planning
   modes; no read tool may be building-only or skill-gated (skill-gating stays for rare WRITE
   tools only).

### P5 — dry-run honesty

1. Dry path performs the same tool-existence check as the live path (`runner.py:690-697`
   logic); a missing tool fails the dry run with the same error the live run would give.
2. Stub payloads become self-describing:
   `{"_dry": true, "_note": "simulated — tool was NOT called"}` so a model reading transcripts
   cannot mistake stubs for evidence.

### P5b — typed trigger inputs + loud conditions (live-confirmed 2026-09-03)

Run 04e14a83 (candidate-intake, 15:14 UTC) proved two engine defects with stored data:

1. **Trigger-mapped inputs bypass the input schema.** `{{ event.payload.itemId }}` native-
   evaluates Monday's numeric itemId to `int`, while the stored run inputs show the
   schema-declared `string` — so conditions compared `str != int` and misbranched
   (`dupe_branch` took THEN on a single self-entry; locally the same expression over the
   stored string data returns False). Fix: coerce trigger-mapped inputs through the playbook's
   declared input schema BEFORE the run starts, and store exactly what runs — stored inputs
   and runtime inputs must be the same objects.
2. **Condition evaluation fails silent.** `_run_condition` → `_eval_expression` non-strict: an
   evaluation exception falls through to `return False`, silently choosing the else branch
   (live `name_check` did exactly this). Fix: conditions evaluate strict like loop
   `over`/`until` already do (006.707 lesson) — an un-evaluable condition fails the step
   loudly with the rendered error, never picks a branch by exception.

### P6 — history integrity

1. Healer keep-rule becomes content-aware: among duplicate version rows prefer the one with
   non-empty code, then non-empty manifest, then oldest; log which rows were dropped and why.
2. `restore_manifest`: a row with a NULL manifest never nulls the live manifest — skip with an
   explicit message naming the nearest version that HAS a manifest.
3. Version mint asserts row-write success (the rowless-v39 class): bumping the counter without
   a snapshot row is an error, not a silent success.

## Tests

- Unit per change above; specifically: failed-run-with-require=False returns no evidence;
  announce text for passed/failed/no-run; approval timeout aborts; mint carries specs and the
  carried flag; version_read/diff on fabricated 3-version history; dry-run on unknown tool
  fails; healer prefers content row; restore_manifest NULL skip; mint row-write assertion.
- Duplicate the existing suite for touched modules; suite green before publish.

## Rollout

1. Bump all three version stamps (in-code PluginManifest authoritative, `__init__.py:622`;
   toml; marketplace) to 0.39.0.
2. Local pytest → push (gh auth switch huemorgan2) → publish to marketplaces.com.ai
   (always-ship-after-push).
3. Upgrade on Scanny via upgrade route, verify with `/api/plugins` (running_version 0.39.0
   after luna 101's provenance fields land; before that, fingerprint check via fly ssh).
4. In the ops chat (identify): confirm `playbook_versions` / `playbook_version_read` visible
   and working; confirm a deliberately failed spec run is reported as failed everywhere.
5. Fix candidate-intake itself: restore v33 live, then repair its real bug
   (dupe_branch → name_check → old_cols_email) with specs proving it, as the first real-world
   exercise of this plan's tooling.
