# Phase 5 — Preflight probes (connections + access)

Target: plugin-playbooks **0.12.0** + luna core **plan 038** (SDK probe
field). Answers "will this playbook's tools actually work when the trigger
fires?" — the piece specs can't cover because specs stub the outside world.

## Reassessed from phase 4

- The gate list is now a proven extension point: `probes` slots in after
  `specs` in `_promote` and the REST promote path, using the same
  `{gate, ok, note}` shape.
- The last-result-cache pattern from `playbook_specs`
  (`last_result/last_run_at`) carries over as its own table here
  (`playbook_probe_results`), since probe results are per-tool, not
  per-playbook.
- Phase-4 lesson on layering: manifest guards intent, specs guard behavior,
  probes guard the environment. Keep them separate gates with separate
  wording — the agent steering messages must name which layer refused.

## 1. SDK addition (luna repo — plan 038, version bump, own commit)

`luna/plugins/base.py`:

```python
class ProbeDef(BaseModel):
    """How to cheaply verify this tool would work right now, without side
    effects. v1 kinds: auth (credential alive), resource_read (target
    reachable/readable). No mutating kinds."""
    kind: Literal["auth", "resource_read"]
    # Either a dedicated async callable () -> {"ok": bool, "failure_class":
    # str|None, "detail": str} ...
    handler: ToolHandler | None = None
    # ... or safe args to invoke the tool's OWN handler with (read-only by
    # the kind contract). handler wins when both are set.
    args: dict[str, Any] | None = None

class ToolDef(BaseModel):
    ...
    probe: ProbeDef | None = None
```

No core behavior change — the field is inert metadata until a consumer
reads it. plugin-playbooks consumes it **duck-typed**
(`getattr(rt.definition, "probe", None)`), so 0.12.0 runs unchanged on
older cores (every tool just reports unprobeable). Luna plan folder
`plans/038-tool-probes/` with PLAN.md + execution_summary.md;
`__version__` bump in the same commit.

Probes for plugin-monday / plugin-browser / slack remain follow-ups in
their own repos. To have ONE live probeable tool for QA, plugin-playbooks
declares a probe on its own `playbook_list` tool (kind resource_read,
handler = trivial DB `select 1` via the session factory) — dogfood, and it
exercises the ✅ path end to end.

## 2. probes.py (new module, plugin side)

- `collect_tools(definition) -> list[str]`: walk the IR — `tool_call`
  steps (including subtask/parallel bodies) + agent-step tool allowlists.
  Sorted, deduped.
- `probe_tool(registry, name) -> {tool, plugin, status, failure_class,
  detail}`:
  - not registered → `failed` / `tool_missing`
  - `effective_policy() == "block"` → `failed` / `blocked`
  - probe present → run it (handler, else tool handler with `args`);
    `ok: False` or raise → `failed` with its `failure_class`
    (`credential_dead | resource_gone | permission | rate_limited |
    unknown`); truthy ok → `ok`
  - no probe → `unprobeable` ("tool present; owning plugin declares no
    probe")
- `run_preflight(session, registry, playbook, definition)`: probe every
  collected tool, upsert `playbook_probe_results`, return
  `{total, ok, failed, unprobeable, results: [...]}`. Caller commits.

## 3. Storage

`playbook_probe_results`: id PK, playbook_id FK cascade, tool String(128),
status String(16), failure_class String(32) nullable, detail Text
nullable, probed_at DateTime. Unique (playbook_id, tool). Feeds UI badges
(phase 6) and the gate note.

## 4. Integration

- Tool `playbook_preflight(name, version="auto")` (skill-gated +
  AUTHORING_TOOLS): target candidate-else-live like spec targeting;
  returns the summary + per-tool lines + steering (`failed` → name the
  tool and class; suggest checking the connector / editing the playbook).
- Promote gate `probes` after `specs` in BOTH paths: refuse only on
  `failed` probes (`unprobeable` passes; note counts, e.g.
  "2 ok · 3 unprobeable"). Refusal shape mirrors specs
  (`gate: "probes"`, `failing_tools: [...]`).
- REST: `GET /playbooks/{name}/probes` (cached rows) and
  `POST /playbooks/{name}/preflight` (run now) for the phase-6 UI.
- **Daily re-probe**: async loop scheduled from the routes startup hook
  (same pattern as the initial binding sync — on_load's loop dies under
  `luna serve`). Every 24h: preflight each enabled playbook; on a
  transition into `failed`, post a muted message (`channel="moment"`)
  naming playbook, tool, failure class — the "your credential died before
  the trigger fired" alert. No transition → silence.
- Skill: PREFLIGHT section (when to run it, how to read ⚠ vs ❌, that
  promote refuses on ❌ only).

## 5. Verification

- Unit (`tests/test_probes.py`): collect_tools across nested IR;
  probe_tool matrix (missing, blocked, ok handler, failing handler with
  class, args-mode calling the tool's own handler, no probe); preflight
  upsert + re-run updates rows; promote gate refuses on failed and passes
  with unprobeable-only; gate note wording; REST endpoints; tool policies
  + gating; daily-loop transition detection (call the loop body directly
  with a fake clock — no real sleep).
- Full suite green (125 existing must stay green — the new gate changes
  the promote gates array again: update phase-3/4 assertions once).
- Live QA (:8766): rsync BOTH luna core (probe field) and the plugin;
  restart. Turn J: agent runs playbook_preflight on qa-code-hello →
  `send_chat_message` unprobeable, `playbook_list`-style self-probe ok if
  referenced; DB rows verified. Turn K: promote a candidate → gates show
  `probes` passing with the note. REST curls for both endpoints. Manually
  invoke the re-probe loop body once and verify rows + (if a failure is
  forced by pointing a copy at a missing tool via direct DB edit) the
  muted-message alert.
- Ship: luna commit (plan 038 + bump) stays LOCAL unless user pushes core
  separately (luna repo push policy: main, but publish flow is the
  plugin's). Plugin 0.12.0: three stamps, commit, push huemorgan2,
  publish official, catalog check.
