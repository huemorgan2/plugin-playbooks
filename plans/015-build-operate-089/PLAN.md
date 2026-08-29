# 015 — build/operate model for playbooks (luna plan 089)

Implements the plugin-playbooks sub-plan of luna plans/089-build-operate
(chat isolation + build/operate). Binding contracts: 089 PLAN.md
"Cross-cutting contracts" §1–§8. Core P1–P3 (conversation kinds/states, ops
chat discovery, ToolDef.modes filtering) land in the luna repo separately;
everything here is written against those declared contract shapes and
degrades cleanly on cores that predate them.

## What ships (0.26.0)

1. **Publish contract (089 #8)** — `playbook_promote` becomes
   `playbook_publish`. One gated function (`_do_publish`) is the only code
   path that makes a version live, for candidate publishes, restores, and
   rollbacks (rollback = publishing a previous version through the same
   function). Gates, machine-checked in the handler: static validation →
   specs (candidate only) → **test run** → manifest drift (reported) →
   probes. The test-run gate demands a green completed run of the EXACT
   version recorded after the version row was created — version rows are
   immutable (every edit mints a new number), so "since last edit" is
   simply "after PlaybookVersion.created_at". `playbook_run_candidate`
   stamps `is_test=True` and is the evidence supplier. The HTTP
   promote/rollback routes run the same gate via shared `publish.py`
   helpers. Every publish/rollback emits `playbook.published` and posts a
   muted announcement (version + evidence run id) into the ops chat.

2. **Run routing (089 §1)** — `PlaybookRun.report_to` stamped at creation,
   never resolved at delivery: test runs → the chat that started them
   (fallback ops); live runs → the ops chat, even when a bus event fired
   inside a chat turn; the one exception is a live run the agent starts
   explicitly in a chat (`agent`/`subtask:*` triggers), which reports where
   it was asked for. On cores without an ops chat, report_to stays NULL
   and delivery behaves exactly as 0.25.

3. **Trigger hygiene (089 §6)** — subscriptions pass `background=True`
   (TypeError fallback for old cores) so a bus event fired inside a chat
   turn can't start a run within that turn's context; identical concurrent
   deliveries (same playbook + event + mapped inputs) single-flight while
   the first run is alive.

4. **Fix proposals (089 §4)** — `FixProposalService` watches
   `playbook.run.completed`; a failed LIVE run of the CURRENT live version
   files one open `playbook_fix_proposals` row per (playbook, failure
   signature) and posts an approval-shaped card into the ops chat via
   `ctx.approval.request`. Repeats bump `failure_count`. Approval wakes the
   ops chat (muted moment, respond=True) to fix through mode-gated tools.
   Signature: sha1(playbook | failed step | error head, digits stripped).

5. **Modes + prompts (089 §5)** — every ToolDef declares `modes` (core
   filters at registry assembly once P1 lands; the pre-089 SDK drops the
   kwarg harmlessly). Read-only tools: all five states; publish/rollback/
   run: building + fix_publish; ack_failures: everything but planning.
   `prompt_sections(kind, state)` renders ops-mode sections, drops the
   "MUST use playbooks" rule while planning, softens it in building chats,
   and keeps the failure digest out of building/planning chats.

6. **Autonomy (089 §3)** — `Playbook.publish_autonomy` ('ask'|'auto') via
   `playbook_set_autonomy(name, agent_autonomy=, publish_autonomy=)`.

## Recorded deviations (plan says "use judgment, record it")

1. **Chat-invoked live runs report to the invoking chat, not ops.** A user
   asking "run X" in a building chat gets the result where they asked.
   089 §1's "live → ops" is applied to background (trigger/cron/bus) runs.
2. **No contextvar pin on pre-P2 cores.** The runner pins the delivery
   conversation via the declared `ctx.pin_conversation` seam only when the
   core provides it. An earlier draft poked
   `luna.agent.runtime._current_conversation_id` directly; removed — the
   package has a no-core-imports invariant (SDK-only), and inventing a
   core-internals seam violates the 089 ground rule. Until core P2,
   delivery relies on the explicit conversation_id the runner threads into
   step contexts.
3. **`playbook_publish` stays `policy="prompt_always"`.**
   `publish_autonomy='auto'` cannot suppress the approval card without a
   core standing-approvals API; the tool result and set_autonomy note say
   so. 'auto' becomes meaningful when core ships that surface.
4. **Restores/rollbacks skip the specs gate and accept live history as
   test evidence** (`include_live=True`). Old versions routinely predate
   newer specs, and a previously-live version's production record IS its
   evidence. Static validation and probes still run.
5. **Fix-proposal cards are state-agnostic and availability-gated.** No
   SDK surface reads the ops chat's state, so the card posts in any ops
   state; on cores without ops chat/approvals the ledger row still lands
   (dedupe + digest keep it visible).
6. **No in-handler state refusals.** Mode gating is core's job via
   `ToolDef.modes` filtering (089 P1); handlers don't second-guess the
   registry. Pre-089 cores therefore expose all tools everywhere — same
   as 0.25 behavior.
7. **TurnRegistry / work-registry integration deferred** to core P5
   (surface doesn't exist yet).
