# 014 — failed-run awareness: the agent owns playbook failures, without interruption

Status: PLANNED 2026-08-28 (simplified same day — see "Cut on review").

## Problem

When a playbook run fails (cron/webhook/trigger runs especially — no
conversation attached), the agent never learns about it. 100 runs can fail
silently after an edit. The owner wants the agent to know, own it, and ask
the owner what to do.

Hard constraint from the owner: **no interrupting messages.** The existing
pattern — `ctx.send_muted_message(channel="moment")` (used by the probe
sweep, [routes.py:174](../../plugin_playbooks/routes.py#L174)) — spawns a
muted turn that fires the agent into action mid-whatever-it-was-doing.
Explicitly rejected here.

## Approach: ambient digest via `prompt_sections()`

Luna re-queries every plugin's `prompt_sections()` on each agent turn
(luna/agent/runtime.py, 008.9 C.3 — concurrent gather). The plugin already
contributes the playbook list there. We append a second, conditional
section: a **failing-playbooks digest**. The agent sees it at the start of
its next natural turn and raises it with the owner at a natural moment.
Nothing is pushed; no turn is spawned.

"Since the last change" falls out of data we already have:
`PlaybookRun.playbook_version` vs `Playbook.live_version` — runs always
execute the live version, and an edit+promote creates a new one. Counting
failed runs **of the current live version** is exactly "since the last
change", and the counter resets automatically when the agent or owner
ships a fix.

## Changes (all in plugin-playbooks; no core changes)

### 1. Model: one nullable int column

`Playbook.failures_acked_version: int | None` — additive column,
`checkfirst` create as usual. Ack is **per version**: "the owner decided
about this version's failures." Predicate is trivial
(`failures_acked_version != live`), and once acked the digest stays silent
until the playbook actually changes — no re-nag on the next failure of the
same known issue. Tradeoff accepted: "it's still failing a week later"
will not resurface on its own; the owner said ignore, we ignore.

### 2. Prompt section (query inline in `prompt_sections()`)

One additional grouped query next to the existing playbook-list query
(the `(playbook_id, started_at)` index covers it; no cache — the section
already hits the DB uncached every turn):

- scope: enabled playbooks, `playbook_version == (live_version or
  version)` (legacy rows: `live_version == 0` means "same as version"),
  `status = 'failed'`, `failures_acked_version != live`
- per playbook: failed count, total run count in same scope, first/last
  failure timestamps, last failed `run_id`
- candidate runs are excluded by construction (their `playbook_version`
  is the candidate number, not live); `cancelled` ≠ failed. At
  implementation time, verify dry-run/spec paths don't write
  `playbook_runs` rows (they shouldn't — specs run against the trace).

Rendered only when non-empty:

```
## Playbook failures needing your attention
- `daily-report`: 47 of 47 runs FAILED since its last change (v12,
  promoted 3 days ago). Last failure 20 minutes ago
  (run_id 7f3a…, inspect with playbook_status).
```

Followed by fixed instructions:

- You own these. First call `playbook_status(run_id)` to see what broke,
  then tell the owner in the next normal conversation turn — after
  finishing whatever they asked for, not instead of it. Ask what to do;
  offer: fix (`playbook_edit` → promote), disable, or dismiss
  (`playbook_ack_failures`).
- Never derail a muted/trigger turn for this.
- All ages are server-computed relative strings ("3 days ago") — the agent
  has no clock; never emit raw timestamps for it to do math on.

### 3. New tool: `playbook_ack_failures(name)`

Sets `failures_acked_version = live`. Auto-approve, low risk, ungated (it
must be callable in the same turn the owner says "ignore it").
Description states the contract: "call only after the owner has decided;
an edit that promotes a new version re-arms the digest by itself."
`playbook_*` names are plugin-local — no global collision.

## Cut on review (had them, dropped them — no goal lost)

- **`playbook_list` enrichment** — the digest already carries the counts;
  `playbook_status` has the details. Redundant surface.
- **Separate `failures.py` module + 20s cache** — one grouped
  index-range query per turn needs neither; premature.
- **Last-error snippet in the digest** — the agent must read
  `playbook_status` before diagnosing anyway; a 200-char snippet invites
  it to speculate instead. The digest hands it the `run_id`.
- **Timestamp-based ack** — re-raises after every new failure of an
  already-dismissed issue; version-scoped ack matches "since the last
  change" and stays quiet.

Also not in scope: UI work, thresholds/rate logic, failure
classification, any new muted messages.

## Tests

- Digest query: version scoping (old-version failures excluded), ack
  predicate, candidate-run exclusion, cancelled excluded,
  `live_version == 0` legacy rows.
- Prompt section: absent when clean; renders counts/ages/run_id when
  failing; instructions present.
- Ack silences; a promote (new live version) re-arms without any write
  to the ack column.

## Ship checklist

- Version bump in all three stamps (in-code `PluginManifest` is
  authoritative).
- Verify on the real QA Luna (port 8766): seed a playbook, fail it twice
  via trigger, confirm the digest appears on the next chat turn, agent
  raises it, ack clears it. Sync `~/.luna/managed_plugins` if overriding.
- Publish to marketplaces.com.ai after push.
