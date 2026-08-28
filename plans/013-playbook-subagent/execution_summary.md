# Plan 013 — playbook sub-agent: master execution summary

Executed 2026-08-28 in four phases on branch `013-playbook-subagent`
(worktree), merged to main at plan close. Ships as **plugin-playbooks
0.25.0** (phase 1 was stamped 0.24.0 but 0.24.0 was taken by plans/011's
icons on main; the branch rebased and everything from this plan ships
under 0.25.0). Published to marketplaces.com.ai.

## What the plan delivered

An in-plugin background delegate for playbook work, with a live progress
card in chat:

- **`playbook_agent` / `playbook_agent_status` tools**
  (`delegation.py`, phase 1): one contained `ctx.agent.run_turn`
  (max_turns 40, timeout 900 s, no memory) with an explicit tool
  allowlist — authoring tools + the target playbook's own tools — which
  bypasses skill-gating (luna 046). Duck-typed pydantic-ai event feed →
  `{ts, phase, kind, label, detail, ms}` rows, phases in owner words
  (Understand/Change/Prove/Ship), throttled DB flush + live in-process
  feed, orphan sweeper on load, `playbook_delegations` table.
- **Live progress card** (`card.py`, `routes.py`, phase 2): srcdoc HTML
  card posted via `ctx.post_chat_card`, polling an unauthed
  capability-token route (compare_digest, 404-no-oracle, result withheld
  while running). Phase rows with real step counts, collapsible feed,
  result panel. No iframe-src, same pattern as the inline-code widget.
- **Approval parking surfaced** (phase 3): PARKED derived from the feed
  (last event = gated tool call, unresolved, >8 s), amber card banner and
  status-tool PAUSED message speak owner words ("make the change live"),
  never tool codes. Drift tests pin the gated set and the owner-words map
  to the actual prompt_always ToolDefs.
- **Skill steering** (phase 4 find): playbook-delegation is the DEFAULT
  skill for create/fix/change jobs; playbook-authoring presents itself as
  the inline build-it-together path. The old authoring description
  claimed every modify job and beat delegation at its own scenario in the
  dojo; fixed at the vocabulary level, regression-tested.

## Verification

Every phase verified on a real QA Luna (0.84, port 8766) with real-browser
dojo runs via CDP, per verify-plugins-on-real-luna:

- Phase 1: delegation runs end-to-end via API turns.
- Phase 2: card streams live in the browser without reload.
- Phase 3: three park/approve/resume cycles in the browser; approval via
  chat card click and via API.
- Phase 4: headline scenario — broken `candidate-intake` playbook, natural
  phrasing, delegate diagnoses → edits → proves (2/2 specs) → parks on
  promote → owner approves in chat → v7 live. Baseline vs delegated:
  **7 vs 2** owner-context tool calls, **28,544 vs 19,512**
  `context_input_tokens`.

Final test suite: **252 passed**. Real-Luna finds that unit tests missed:
pydantic-ai≥2 result-event shape, throttled-flush hiding the park, the
skill-description steering miss — the dojo runs paid for themselves three
times over.

## Where the details live

- `phase-1-delegation-core/execution_summary.md`
- `phase-2-progress-card/execution_summary.md`
- `phase-3-approval-parking/execution_summary.md`
- `phase-4-dojo-end-to-end/execution_summary.md`
