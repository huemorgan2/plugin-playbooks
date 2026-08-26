# Phase 6 — UI: trust surface, tabbed editor, run view — execution summary

Shipped as **0.13.0** (ui-src 0.3.0). 147 pytest + 33 vitest green; full live
browser QA on QA Luna :8766 via CDP.

## What was built

### Backend (routes.py)
- `_trust_summaries(session, playbook_ids)` — batched GROUP BY summaries for
  the list endpoint: specs (total / failed-from-last_result / last_run_at) and
  probes (total / failed / probed_at). Wrapped in try/except so trust can never
  break the list.
- List rows now carry `trust: {specs, probes, manifest_present}`.
- GET detail now returns `candidate_definition` / `candidate_code` (from the
  candidate version row) so the UI can show the pending candidate without a
  second fetch.

### UI (ui-src)
- **trust.ts** — one vocabulary module: chip labels (`specsLabel`,
  `probesLabel`, `intentLabel`), Tests-tab headlines (`specsHeadline`,
  `probesHeadline`), and `failureWords` mapping protocol failure classes to
  plain words (`tool_missing` → "tool not installed"). No protocol codes reach
  the screen.
- **List** — trust row under each playbook (tests N/N · tools ok · intent ✓,
  tone-colored text, no chip fills) plus a violet outline chip
  "candidate vN — review & promote" when one is pending.
- **Editor rewrite** — five tabs: Canvas | Code | Manifest | Tests | Runs
  (drafts keep Canvas|Code only). Run *replay* (timeline scrubber,
  StateVizPanel, PlaybookRuns page) deleted entirely.
  - Header: violet "Promote candidate vN" button → REST promote → gates; a
    422 refusal renders a dismissible banner with the gate's message.
  - Canvas: Live/Candidate pill switch; "Past run" banner with clear-X when a
    run is projected onto the graph (status-colored nodes, no animation).
  - Code tab: read-only pblang with Live/Candidate pills and the note "Luna
    writes this — ask her in chat to change the playbook."
  - Manifest tab: plain-text intent, Save bumps the live version.
  - Tests tab: TESTS (spec rows, Run all) + CONNECTIONS (probe rows, Check
    now), eyebrow → headline → rows per ux_guidelines.
  - Runs tab: stats bar, expandable run rows → step execution rows (status
    dot, kind, cost, duration, expandable inputs/outputs/error), "Show on
    canvas".

## Live browser QA (all passed)
Chrome + CDP :9222 against QA Luna :8766 (plugin UI is an **iframe** — drive
it via `iframe.contentDocument`/`contentWindow`, not the top document):
1. List: trust rows on all playbooks, candidate chip on qa-code-hello.
2. Editor opened via `luna:playbook-open` (dispatched on the iframe window).
3. Canvas Live v8 / Candidate v9 switch.
4. Code tab shows candidate pblang (v9 description) with source pills.
5. Manifest: edit → Save enabled → saved → header bumped v8→v10 live
   (manifest save version-rows past the candidate counter, candidate kept).
6. Tests: Run all + Check now both refresh ("just now"), spec row expands to
   the test definition; probes headline honest — "Nothing verified yet — no
   tool answers probes" for send_chat_message (unprobeable).
7. Runs: 1 Total/1 OK stats, run expands to greeting (Llm Step $0.0003 1.2s) +
   notify (Tool Call 15ms); Show on canvas → PAST RUN banner + green status
   dots; clear-X restores the clean graph.
8. Promote candidate v9 from header: gates ran and passed → live v9,
   candidate cleared, button gone (API-verified).
9. Regression: agent `playbook_propose` turn auto-followed into the editor and
   rendered the new playbook live (build path intact). Running badge path
   untouched since 0.12.0 (runs finish in ~1.3s — not re-raced).

Fix found during QA: promote button wrapped to two lines at narrow widths →
`whitespace-nowrap shrink-0` added.

## Learned
- The playbooks UI lives in an iframe on /p/playbooks — all previous CDP
  helpers targeting the top document silently no-op. Recorded for future QA.
- `put_manifest` intentionally versions past a pending candidate (live v10 >
  candidate v9); promote afterwards sets live_version back to the candidate's
  number. Confusing to read but correct — version is a counter, live_version a
  pointer.

## Reassessment of phase 7
Unchanged in scope, plus QA leftovers grew: delete `qa-p6-glow` and the
"(edited during phase-6 browser QA)" manifest line on qa-code-hello, along
with the phase0/phase2 test playbooks and the scratch chrome profile.
