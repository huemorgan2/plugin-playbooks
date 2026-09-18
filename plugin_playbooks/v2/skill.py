"""The v2 authoring skill (plans/032 phase 05; docs/v2.md §2, §10, §11).

`V2_SKILL_BODY` is the body of the `playbook-authoring-v2` SkillDef. It is
written from `docs/v2.md`: the ctx table condenses §2, the two ```python
blocks are the doc's two blocks byte for byte (`tests/test_v2_skill.py`
compares them), the honesty rules are the v1 skill's sentences verbatim.
`PUBLISH_RULE` is the owner-intent sentence the `playbook_publish` tool
description carries too.
"""

from __future__ import annotations

V2_SKILL_MAX_BYTES = 6144

PUBLISH_RULE = (
    "Publish only when the owner asked for this change to go live; a green "
    "candidate run is evidence, not permission."
)

V2_SKILL_BODY = '''\
## Playbook Authoring (python)

A playbook is ONE Python file with exactly one `async def run(ctx, inputs)`.
Plain Python inside: loops, `try`/`except`, comprehensions, helpers. Effects
are the ONLY way to touch the world, every effect is `await`ed on `ctx`, tool
and playbook names are string literals, and `inputs` is a plain dict
(`inputs["url"]`). Pass `_id="name"` on every effect: it is the journal and
stubs key.

| Effect | Returns | Raises |
|---|---|---|
| `ctx.tool(name, /, **args)` | the tool result, unwrapped | `ToolError`, `EffectTimeout`, `OutcomeUnknown` |
| `ctx.llm(prompt, *, output=None, purpose=None, model=None, system=None)` | `dict` with `output=`, else `str` | `EffectError`, `EffectTimeout` |
| `ctx.agent(prompt, *, output=None, tools=None)` | `dict` with `output=`, else `str` | `EffectError`, `EffectTimeout`, `OutcomeUnknown` |
| `ctx.subtask(playbook, inputs=None, *, returns=None)` | the child's return value (or the picked keys) | `SubtaskFailed`, `EffectTimeout`, `OutcomeUnknown` |
| `ctx.gather(*effects)` | results in argument order (un-awaited effect calls only) | the first failure, after all settle |
| `ctx.approve(*, show)` | `{"approved": True, "request_id", "reason", "decided_by"}` | `Rejected`, `ApprovalExpired` |
| `ctx.now()` / `ctx.random()` / `ctx.log(msg)` | UTC `datetime` / `float` in [0,1) / `None` — all journaled | — |
| `ctx.wait_event(...)`, `ctx.sleep(...)` | not available in this version | — |

Options: `_id`, `_timeout` (seconds), `_retry` (tool/llm/agent). Catch
failures as `ctx.ToolError` etc.; `RunCancelled`, `JournalDivergence`,
`MaxEffectsExceeded` cannot be caught. Imports: whitelisted stdlib only; no
classes, no I/O, no `print` — `await ctx.log(msg)` is the only stdout.

FAILURE PATH: when the owner wants the run to stop on a failed effect,
write it explicitly — `try:` around the effect(s), `except ctx.ToolError
as e: raise ValueError(f"<what> failed: {e}")` naming the item — and say
so in the reply. An uncaught error also fails the run, but without a
message naming what failed; the explicit raise is what was asked for.

### THE LOOP v2
Never run blind:
1. WRITE: `playbook_propose(name, code=...)` creates; `playbook_edit` (read,
then write with the ticket) changes. Both compile + check in one call; a
green write carries `"validated": true` — do not call `playbook_validate`
after it. A write saves a CANDIDATE (not live).
2. DRY RUN: `playbook_dry_run(name, inputs, stubs)` runs the candidate on
the real loop with every effect stubbed: unstubbed results are placeholders
(truthy, iterate once); `stubs` is keyed `"<id>#<n>"` per occurrence or
`"<id>"` for all. Read `steps_ran` and `unreached_call_sites`; a
`DryStubError` names the stubs key to add. Outputs are SIMULATED: NEVER
report a dry-run value as a real result.
3. RUN: `playbook_run_candidate(name, inputs)` — the REAL supervised proof
(asks the owner). Runs execute in the background: on 'running', poll
`playbook_status(run_id)` until 'done'/'failed'; never re-run a 'running'
playbook or invent results.
When you report a run, quote its `kind` and `version` from the result —
'real run of v3', 'candidate test run of v4', 'dry run of v4 — simulated,
no side effects'. A dry run is never 'a run'. `playbook_overview(name)` is
the truth surface — read it before describing a playbook's state.
4. PUBLISH: `playbook_publish(name)` — gates: static check, a green
candidate run since the last edit, tool probes. `playbook_rollback` restores
the previous live version.

### CANDIDATE vs LIVE
A write saves a candidate; the live version keeps running unchanged.
Triggers and `playbook_run` use the live version only.
`playbook_dry_run` and `playbook_run_candidate` exercise the candidate.
`playbook_publish` makes the candidate live after the gates pass.
NEVER report an edit as done after `candidate_saved` — the old version runs
until publish succeeds.

For a multi-file summary, calculate totals from real source records inside
the saved playbook; do not hand-copy figures in chat. After a real run,
reopen the persisted summary and check exact keys, counts and arithmetic
before claiming it is correct.

''' + PUBLISH_RULE + '''

### WHERE IT RUNS
The playbook body runs in a jail, one segment per effect; the host journals
and executes each effect (tools, llm, agent, subtask); `approve` parks the
run until the owner decides. Effects are replayed from the journal, so a
value once drawn (`now`, `random`, `llm`) stays fixed. Cap: 200 effects.

### EXAMPLES
```python
async def run(ctx, inputs):
    rows = await ctx.tool("fetch_list", url=inputs["url"])
    good = [r for r in rows["items"] if r["score"] > 3]
    summaries = []
    for r in good:
        s = await ctx.llm(f"Summarize {r['title']}", output={"s": "str"})
        summaries.append(s["s"])
    await ctx.approve(show=summaries)
    await ctx.tool("send_message", to=inputs["owner"], text="\\n".join(summaries))
    return {"count": len(summaries)}
```

```python
async def run(ctx, inputs):
    queue = list(inputs["urls"])
    pages = []
    failed = []
    while queue:
        url = queue.pop(0)
        try:
            page = await ctx.tool("fetch_page", url=url, _retry=2)
        except ctx.ToolError as e:
            failed.append({"url": url, "error": str(e)})
            continue
        pages.append(page)
        for link in page.get("links", []):
            if link not in queue and link not in [p["url"] for p in pages]:
                queue.append(link)
    if not pages:
        raise ValueError("nothing fetched: " + ", ".join(f["url"] for f in failed))
    summaries = await ctx.gather(*[
        ctx.llm(f"Summarize {p['text']}", output={"s": "str"}, _id="summary")
        for p in pages
    ])
    report = "\\n".join(s["s"] for s in summaries)
    await ctx.tool("send_message", to=inputs["owner"], text=report)
    return {"pages": len(pages), "failed": failed}
```
'''
