# Phase 6 — UI: trust surface, tabbed editor, run view (replay removed)

Ships as **plugin-playbooks 0.13.0** (backend additions + rebuilt `ui/`).
UX contract: `/vision/ux_guidelines.md` — every section is eyebrow →
bottom-line headline → one-line rows → expand on demand; no jargon; state
chips recolor border+text, never fill.

## Reassessment going in (from phase 4+5)

- GET `/playbooks/{name}/specs`, POST `/specs/run`, GET `/probes`,
  POST `/preflight`, GET/PUT `/manifest` all exist and are UI-shaped — the
  Tests and Manifest tabs are pure frontend.
- What's missing server-side is LIST-level trust data (badges without N+1
  fetches) and the candidate's definition for the canvas switch.

## Backend (routes.py)

1. `GET /playbooks` — each row gains `trust`:
   `{specs: {total, failed, last_run_at}, probes: {total, failed, probed_at},
   manifest_present: bool}`, from two batched GROUP BY queries (never N+1,
   never fail the list).
2. `GET /playbooks/{name}` — when a candidate exists, add
   `candidate_definition` and `candidate_code` (canvas live/candidate switch;
   Code tab shows the candidate's source when inspecting it).

## UI

**List (PlaybooksSection)** — trust row under the existing meta line:
- `tests 4/4` (green; red `1 failing`; amber `untested`)
- `tools ok` / `N broken` (red) / `unchecked` (amber), with age
- `intent ✓` / amber `no intent`
- violet chip `candidate v13 — review & promote` when candidate_version set.

**Editor (PlaybookEditor)** — view modes become tabs:
`Canvas | Code | Manifest | Tests | Runs` (drafts keep Canvas | Code only).
- *Canvas*: unchanged graph + a Live vN / Candidate vM switch when a
  candidate exists (renders candidate_definition).
- *Code*: pblang source (was the JSON "YAML" tab; shows `code`, falls back
  to pretty JSON IR). Read-only — the agent is the writer.
- *Manifest*: free-text editor + Save (PUT manifest), "no intent written
  yet" empty state. This is the vision page for the playbook.
- *Tests*: two sections. TESTS — headline verdict ("4/4 passing" /
  "1 failing" / "No tests yet"), one row per spec (pass/fail dot, name,
  age) expanding to the spec YAML + last failure diffs; Run-all button.
  CONNECTIONS — headline ("All 3 tools answered" / "2 broken" / "Nothing
  verified yet"), one row per probe (status dot, tool, detail), Check-now
  button. Failure classes render as plain words ("credential dead").
- *Runs*: the run list moves from side panel into the tab, each run
  expands to a **step execution list** (step id, kind, status, duration,
  cost, retries → resolved inputs / outputs / error on expand). "Show on
  canvas" per run keeps the status-colored graph.
- Header: existing meta + `Promote candidate` button when one exists
  (failing gate named inline on 422) + rollback stays in Versions panel.

**Replay removal**: delete `runReplay.ts`, `StateVizPanel.tsx`,
`ReplayToggle`, timeline/cursor/playing state, the ▶ affordance in the runs
list, and `runReplay.test.ts`. Live runs still color the canvas via the
existing status refresh — they just don't "animate".

## Verification

- vitest suite (update for removals; add trust-label unit tests).
- `npm run build` → ships `ui/`; python tests still green.
- Live on QA :8766 with the real browser (CDP :9222): list badges render,
  tabs switch, manifest saves (version bump visible), Tests tab runs specs
  and preflight, candidate promote from header hits the gates, runs tab
  shows step executions. Regression: agent live-patch still animates the
  canvas (build glow), running badge still appears.

## Ship

Bump three stamps to 0.13.0, commit, push (huemorgan2), publish official,
catalog check, execution_summary.md, reassess phase 7.
