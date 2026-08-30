# 016 — Versions as the playbook view (+ silent-promote fix)

Target: plugin-playbooks **0.28.0** (UI rework) with a **0.27.1 hotfix** for the
promote bug shippable first, on its own.

Baseline: 0.27.0 (`fa9615e`). Local checkout was 45 commits behind origin and
was fast-forwarded before this plan was written; luna `plugin-set.toml` pins
0.26.0, which has the same promote/versions code paths as 0.27.0.

---

## Part A — "Promote did nothing" on `monday column discovery` (bug)

### Root cause (verified in code, not reproduced live — the devtools browser
### has no luna.com.ai session)

Two defects stack:

1. **The UI swallows the refusal.** `VersionsPanel.handlePromote`
   (`ui-src/src/playbooks/PlaybookEditor.tsx:938-946`) does
   `catch { setPromoting(null) }` — any non-2xx from `POST /promote` is
   discarded. The header's candidate-promote path already has
   `promoteRefusalMessage()`; the versions panel never got it.

2. **The restore gate can never pass for most historical versions.**
   `POST /playbooks/{name}/promote {version: N}` (`routes.py:1012-1120`)
   runs `test_run_gate(..., since=row.created_at, include_live=True)`.
   `latest_run_evidence` only accepts runs with
   `started_at > row.created_at`. But a version row's `created_at` is the
   moment the row was *snapshotted*, and snapshots are written **at the
   next edit** ("before whole-YAML edit", "before promoting vX",
   `_ensure_live_row` "live content (recorded on first candidate/promote)").
   So every run version N ever had predates its own row → "no test run
   since its last edit" → HTTP 422 → swallowed by (1). Net effect: click,
   spinner flickers, nothing. Exactly what was reported.

   Version rows are immutable (every edit mints a new number), so for a
   restore the `since` bound is meaningless: *any* completed run of
   version N ran exactly that content.

### Fix (0.27.1)

- `publish.py::test_run_gate` / `latest_run_evidence`: when
  `include_live=True` (restore/rollback), pass `since=None`. Same change in
  the tool path `agent_tools.py` `_do_publish` restore/rollback
  (`~line 1711`) so the HTTP route and the `playbook_publish` tool agree.
- Candidate publishes keep the strict "tested since edit" rule (unchanged).
- UI: `VersionsPanel.handlePromote` surfaces `promoteRefusalMessage(e)`
  inline (same rose banner as the header) instead of swallowing. This is
  temporary — Part B replaces the panel — but it ships with the hotfix so
  the owner sees *why* when a restore is refused (e.g. a version that never
  ran: "Run v7 as a test first").
- Tests (`tests/`): restore of a version whose only green run predates its
  row `created_at` → 200; version with zero runs → 422 with
  `gate: test_run`; rollback path same; candidate path still requires a
  run after `created_at`.
- Bump 0.27.1 in `luna-plugin.toml`, `pyproject.toml`, `__init__.py`
  manifest (same commit), rebuild `ui/`, publish (`publish-plugin` skill),
  then repin in luna `plugin-set.toml`.

---

## Part B — Versions becomes the playbook view (0.28.0)

### UX contract

**Top-level tabs** (non-draft playbook): `Versions` · `Settings`. Everything
else — Canvas, Code, Manifest, Tests, Runs — is a view of the *selected
version* inside Versions (runs are already stamped `playbook_version`;
tests become per-version in Part C).
The icon-only History toggle (`PlaybookEditor.tsx:519-531`) and the
320px side `VersionsPanel` are removed; `Versions` is a normal `TabBtn`
(History icon + word) like the others, and it is the **default tab**.
Drafts (no version history) keep today's `Canvas` / `Code` tabs untouched.

**Versions tab = full section, two panes:**

```
┌──────────────────────────────────────────────────────┬──────────────────┐
│ v12 · Aug 29, 2026 14:03 [Canvas|Code|Manifest|Tests|Runs] │ v13  Candidate ↗│
│                                       ● Published · Live │  v12  Published·Live│
│──────────────────────────────────────────────────────│  v11             │
│                                                      │  v10  ← from v8  │
│           canvas / code / manifest of v12            │   …              │
│                                                      │                  │
└──────────────────────────────────────────────────────┴──────────────────┘
```

- **Right pane — version list** (newest first, from `GET /versions`):
  row = `vN` bold, badge **top-right** of the row: `Published · Live`
  (green) for the live version, `Candidate` (violet) for
  `candidate_version`, none otherwise; then author · date · run count;
  `← promoted from vX` when set. The word "current" disappears. Click
  selects. **Initial selection = the live version.** Selection is shown by
  **highlighting the row** (the `bg-luna-600/20` + `text-luna-400` active
  style the tab strip already uses, plus a left accent border) — never by
  writing "selected" on it. The only text badges a row can carry are
  `Published · Live` and `Candidate`.
- **Left pane — selected version.** Toolbar, left → right:
  1. `vN` — large, bold.
  2. created date (absolute; `title` = time-ago).
  3. segmented switch `Canvas | Code | Manifest | Tests | Runs`.
  4. right-most slot: live version → static green badge
     `Published · Live`; any other version → button **`Promote to live`**
     (Rocket). Candidate row → same button, calls the gated candidate
     endpoint (`promoteCandidate`, body `{}`); older rows → `promoteVersion(N)`.
     Refusal renders inline under the toolbar via `promoteRefusalMessage`.
     While promoting: spinner + disabled. On success: reload playbook, list
     re-fetches, selection moves to the newly live version, badge flips.
- Content below the toolbar: the version's Canvas (read-only graph),
  Code (read-only pblang/JSON), Manifest, Tests, or Runs. Manifest is
  **editable only for the live version** (reuses `ManifestTab`; saving
  bumps the live version exactly as today); other versions show their
  snapshotted manifest read-only. **Tests** = `TestsTab` filtered to that
  version's specs (`?version=N`, Part C); "run all" evaluates them against
  that version's content. **Runs** = `RunsTab` filtered to
  `playbook_version == N` (`GET /runs?version=N`); "show on canvas" flips
  the same toolbar to Canvas and overlays the run — it never leaves the
  version. Live runs streaming in (`activity.*`) land on their version's
  row count and on its Runs view.
- The header (`PlaybookEditor` top bar) loses the `Promote candidate vN`
  button and the candidate/live canvas switch — both are now expressed by
  the versions list + toolbar. The header keeps name and the two-tab strip
  (`Versions` · `Settings`).

### Backend

- **New** `GET /playbooks/{name}/versions/{n}` → `{version, definition,
  code, manifest, author, message, created_at, promoted_from, live,
  candidate, runs}`. Legacy live version without a row (the synthesized
  entry in `list_versions`) is served from the `Playbook` row itself.
- `list_versions` unchanged (already returns `live` / `candidate`).
- `GET /playbooks/{name}/runs` gains `?version=N` (filter on
  `PlaybookRun.playbook_version`); `GET /specs`, `POST /specs/run` gain
  `?version=N` (Part C).
- No schema changes.

### Frontend (`ui-src/src/playbooks/`)

- `api.ts`: `getVersion(name, n)`.
- **New `VersionsTab.tsx`** — owns list + selection + toolbar + view
  switch + promote state. Props: `name`, `liveVersion`, `candidateVersion`,
  `onPromoted()`, and the run-overlay hooks below.
- **Extract `VersionCanvas.tsx`** from the canvas branch of
  `PlaybookEditor` (`~lines 583-710`): builds nodes/edges with
  `buildGraph(def)`, ReactFlow with `nodesDraggable={false}`, keeps the
  step-click → `StepDetailPanel` behaviour and the run-status overlay
  (`runDetail` → node status colouring) — because **Runs → "Show on
  canvas"** must keep working: it now switches to `Versions`, selects the
  run's `playbook_version`, and overlays that run.
- Code view: reuse the existing read-only code block from the `code`
  branch (`~lines 713-740`); candidate/live toggle removed (the list is
  the toggle).
- `PlaybookEditor.tsx`: `ViewMode` → `'versions' | 'settings'` for
  playbooks; `RunsTab`/`TestsTab` take a `version` prop and are rendered by
  `VersionsTab`, not the editor (`'canvas' | 'code'` stays for drafts). Delete `VersionsPanel`,
  `versionsOpen`, `canvasSource`, `candidateDef/Code` plumbing that only
  fed the old switch. Live agent edits (`livePatch.ts` / `playbook.saved`
  → `loadData()`) refresh the list; if the selected version is the
  candidate that just changed, re-fetch it so the canvas follows the agent
  as it does today.
- `PlaybooksSection` untouched (it already renders the editor full-pane).

### Tests

- vitest: `VersionsTab` — default selection is live; live row shows
  `Published · Live` badge and toolbar shows badge not button; selecting an
  older row shows `Promote to live`; promote on candidate calls
  `promoteCandidate`, on older calls `promoteVersion(N)`; a 422 renders the
  gate name; success re-selects the new live.
- pytest: `GET /versions/{n}` for stored row, legacy synthesized live,
  and 404.

### Ship

- 0.28.0 in `luna-plugin.toml` + `pyproject.toml` + `__init__.py` manifest
  (one commit); `npm run build` → `plugin_playbooks/ui/`; `python -m pytest`
  + `npm test` green; publish via `publish-plugin`; repin luna
  `plugin-set.toml`; verify on
  `luna.com.ai/a/vaselin-luna-bug-fixer/p/playbooks` that restoring an old
  version of `monday column discovery` flips the badge.

---

## Part C — Tests travel with versions; publish gates are owner settings

### C1. Specs are versioned (duplicate-on-mint)

Problem: `playbook_specs` is keyed `(playbook_id, name)` — one flat set
per playbook. Adding a spec for a new feature makes every *older* version
fail that spec, so a restore/rollback of a previously-good version is
impossible under a "all tests green" gate. Tests must belong to the version
they were written against, like code.

Decision: **duplicate all specs whenever a new version number is minted**
(simpler than per-spec version ranges; storage is trivial).

- `PlaybookSpec.playbook_version: int` (new column, NOT NULL). Unique index
  becomes `(playbook_id, playbook_version, name)`.
- Backfill on load (same pattern as `live_version` at `__init__.py:112`):
  existing rows → `playbook_version = live_version`; if a
  `candidate_version` exists, copy them to it too.
- One helper `mint_version(session, p, *, source_version, ...)` becomes the
  ONLY way to create a version number. It snapshots the row **and** copies
  every spec of `source_version` to the new number (fresh `last_*` = NULL,
  they haven't run against it yet). Replaces the five ad-hoc `version += 1`
  sites: `routes.py:738` (PUT definition), `routes.py:1326` (manifest
  save), `agent_tools.py:1375` (candidate save), `:1558` (whole-YAML edit),
  plus `_ensure_live_row`. `source_version` = the version being edited
  from (candidate if the edit is on the candidate, else live).
- Spec tools/routes gain a version target: `playbook_spec_add` /
  `playbook_spec_from_run` / `spec delete` write to the **candidate**
  version when one exists, else to live (matches what the agent is editing;
  explicit `version=` override for restores). `GET /specs` and the Tests
  tab take `?version=N` and default to the version selected in the Versions
  tab (Tests tab shows "Tests of v12" in its header).
- Gates evaluate the specs **of the version being published**
  (`run_all_specs(..., version=row.version)` filters by
  `playbook_version`). Restoring v8 runs v8's specs against v8's content →
  rollback works again. This supersedes plan 015 deviation #4 ("restores
  skip the specs gate").
- Version list rows show `12 tests · 12 green` from that version's spec
  cache; the Versions toolbar Promote button is disabled with a reason
  when the selected version's gates are known-red.

### C2. Publish / Promote settings (per playbook, owner-controlled)

Settings stops being a 320px side panel (`SettingsPanel`,
`PlaybookEditor.tsx:830`) and becomes a **full-page `Settings` tab** with
sections:

```
┌───────────────────────────────────────────────────────────────┐
│ Agent autonomy                                                │
│   ( ) Always allowed  (•) Ask first  ( ) Manual only   (as today)
│                                                               │
│ Publish / Promote settings                                    │
│   Pushing a version requires all tests to be green   [●──] on │
│   Pushing a version requires at least one successful run [●──] on │
│                                                               │
│ Danger zone: Archive                                          │
└───────────────────────────────────────────────────────────────┘
```

- Luna-style on/off switches (same `w-10 h-5 rounded-full` toggle as the
  enable switch in `PlaybooksSection.tsx`). Both default **on**.
- Storage: `Playbook.publish_require_specs: bool = True`,
  `Playbook.publish_require_run: bool = True`. Route
  `PATCH /playbooks/{name}/publish-settings {require_specs?, require_run?}`;
  returned by `GET /playbooks/{name}`. `playbook_set_autonomy` tool gains
  the same two flags so the agent can read/announce them (changing them
  stays `prompt_always`).
- Gate behaviour (route `promote_version`, `rollback_playbook`, and
  `agent_tools._do_publish` — all three, one shared helper in
  `publish.py`):
  - `require_specs` on → **every** spec of the target version must be
    green, for candidate publishes AND restores/rollbacks. Off → specs
    still run and are reported, but do not refuse.
  - `require_run` on → at least one completed **green** run of exactly that
    version (any completed run counts for restores — the Part A fix; test
    runs after edit for candidates). Off → skip the run gate.
  - Static validation and probes are not switchable (a broken definition or
    a known-dead tool is never publishable).
- Refusal text names the gate *and* the setting: "…refused — gate 'specs'
  failed (3 of 12 red). Owner can relax this in Settings → Publish."
- Tab strip for playbooks is just `Versions · Settings` (the ⚙ icon button
  goes the same way as the History icon: a word tab).

### Phases (for `phased-execution`)

1. **Hotfix 0.27.1** — Part A (gate `since=None` for restores, UI surfaces
   refusal, tests, publish).
2. **API** — `GET /versions/{n}` + tests.
3. **Extract** — `VersionCanvas` + read-only code view out of
   `PlaybookEditor`; editor still renders identically (no visible change).
4. **VersionsTab** — list, selection, toolbar, promote, manifest gating;
   wire as top-level tab; `RunsTab` per version (`?version=N`) inside the
   toolbar; remove side panel, header promote button, candidate switch and
   the top-level Tests/Runs tabs.
5. **Versioned specs** — column + backfill, `mint_version` helper replacing
   all five mint sites, spec tools/routes/Tests tab versioned, gates read
   per-version specs. Tests: new version inherits N specs with reset
   results; restore of v8 evaluates v8's specs only; spec added on the
   candidate does not touch live's set.
6. **Publish settings** — columns + PATCH route + tool flags; full-page
   Settings tab with the two switches; gates honour the flags in all three
   publish paths. Tests: each flag off/on × candidate/restore.
7. **Ship 0.28.0** — tests, build, publish, repin, live verification.

### Risks / decisions taken

- Restores accept *any* completed run of the exact version as evidence
  (dropping the `since` bound). Rationale: immutable version rows; the
  bound only made sense for candidates being re-edited in place, which no
  longer happens.
- A version that has **never run** cannot be promoted while
  `require_run` is on (owner decision, 2026-08-30); nor one with red specs
  while `require_specs` is on. Refusals are always visible. A "Run as
  test" button on the Versions toolbar is a follow-up (needs `POST /runs`
  to accept `version` + `is_test`) — noted, not in scope.
- Duplicating specs per version multiplies `playbook_specs` rows by the
  number of versions; specs are small JSON and versions are few, accepted.
- Removing the header `Promote candidate` button changes the current
  0.13.0 flow; the candidate is still one click away (it is the top row of
  the list, badged, selected → `Promote to live`).
