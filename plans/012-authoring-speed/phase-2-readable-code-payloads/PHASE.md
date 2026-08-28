# 012 / phase 2 — readable code payloads

Status: IN PROGRESS 2026-08-28. Baseline: repo at 0.19.0 (b28368e), full
suite green (count recorded in execution_summary).

## Learning carried in from phase 1

- `playbook_get_definition` ALREADY returns raw code as plain text
  ([agent_tools.py:727](../../../plugin_playbooks/agent_tools.py#L727)) —
  the PLAN.md assumption that it also needed conversion was wrong. The
  agent's live-chat fallback to it "worked" precisely because it is
  readable. Only the **edit read stage** wraps code in `json.dumps`.
- Scope therefore narrows to `_edit_impl` modes==0 (the read stage,
  [agent_tools.py:1062-1116](../../../plugin_playbooks/agent_tools.py#L1062-L1116)).

## Scope

Rework the read-stage return of `playbook_edit` from one `json.dumps`
blob into: compact JSON header + framed plain-text blocks with real
newlines:

```
{...header: stage, editing, version, live_version, candidate_version,
 ticket, expires_in_seconds, instructions, manifest_note?...}
--- manifest ---
<yaml-dumped manifest, or "(none)">
--- code (candidate v19) / (live v7) ---
<raw multiline pblang source>
--- end ---
```

- `language_reference` moves OUT of the read-stage JSON: phase 3 will
  replace it with the mini-reference; in THIS phase it is appended as its
  own framed block (`--- language reference ---`) so the recall point
  (plans/003 phase 4) survives unchanged — but printed raw, not
  JSON-escaped.
- Write stages: unchanged (small JSON, no code echo).
- Instructions text updated: "copy exact lines from the code block above
  into old=" (the text no longer lives inside JSON so exact copying works).

## Non-goals

- Shrinking payloads (phase 3), `playbook_get_definition` (already fine),
  any UI change.

## Verification

- New contract test: read stage output starts with a `{...}` header line,
  contains `\n--- code` frame, and the code section contains REAL
  newlines and the exact source (`snippet in payload` where snippet spans
  two lines).
- Existing edit-flow tests updated to parse the new shape (helper:
  split header/frames).
- Full suite green.
- Real-Luna probe (memory: verify-plugins-on-real-luna): on QA Luna,
  call the tool chain read → snippet-edit with `old=` copied verbatim
  from the framed output → save succeeds (no "'old' snippet not found").

## Ship

Minor bump per repo convention (next free minor at execution time; three
stamps: in-code manifest + pyproject; luna-plugin.toml if present).
Push + publish per always-ship-after-push, then upgrade + verify the
live tenant.
