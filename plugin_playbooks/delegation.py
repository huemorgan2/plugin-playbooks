"""Playbook delegation — plans/013, reinstated forward by plans/020.

`playbook_agent(task, ...)` hands a playbook authoring job to a focused
background delegate: one headless agent turn (``ctx.agent.run_turn``) whose
context carries the authoring skill + the task + the target playbook's
manifest — and nothing of the owner's conversation. The main chat keeps ONE
tool call and ONE ~1KB result; the delegate's progress is surfaced by the
chat card, fed from the ``events`` list this module records.

Removed by mistake in the 0.33.0 ops-machine cleanup (fc2e017) — the vision
doc lists this module as load-bearing. plans/020 brings it back adapted to
the current world, not reverted:

- luna 098 collapsed conversation states to planning/building (the old
  fix_approve/fix_publish modes are gone) — ToolDef ``modes`` updated.
- The plan gate (plans/017+): ``playbook_publish`` requires a ``plan_id``,
  so the delegate's allowlist now includes the plan tools and it writes the
  plan row itself.

Core seams used (all shipped, no core changes):
- luna 046/phase03: an explicit ``tools=`` allowlist bypasses skill-gating,
  so the delegate gets the gated authoring tools directly.
- luna 049: ``max_turns`` (hard step budget — breach returns ``{"_aborted"}``),
  ``timeout_s``, and ``event_stream_handler`` (the live event feed).
- luna 0.40.003: headless tools run the same dispatch gate as chat, so
  ``prompt_always`` tools (publish, spec_delete) still raise approval cards.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from luna_sdk import ToolDef

from .definition import PlaybookDef, StepDef
from .models import Playbook, PlaybookDelegation

log = logging.getLogger(__name__)

# Keep strong refs — a bare create_task is GC-bait (same pattern as runner.py).
_TASKS: dict[uuid.UUID, asyncio.Task] = {}

_WAIT_DEFAULT = 25.0
_WAIT_MAX = 90.0
_MAX_TURNS = 40          # hard step budget (049 request limit)
_TIMEOUT_S = 900.0       # wall-clock cap for the whole delegation
_RESULT_CAP = 800        # chars of delegate report returned to the MAIN turn
_FLUSH_INTERVAL_S = 1.0  # event-feed DB writes are throttled to 1/s

# ---- phase vocabulary --------------------------------------------------------
# Owner words, never internal codes (the enum values ARE the card labels).
# Inferred server-side from the tool name — the model never emits phases.
PHASES = ("Understand", "Change", "Prove", "Ship")

_PHASE_BY_TOOL = {
    "playbook_get_definition": "Understand",
    "playbook_list": "Understand",
    "playbook_status": "Understand",
    "playbook_language_reference": "Understand",
    "playbook_spec_list": "Understand",
    "playbook_plan_read": "Understand",
    "playbook_propose": "Change",
    "playbook_edit": "Change",
    "playbook_edit_force": "Change",
    "playbook_manifest_set": "Change",
    "playbook_set_autonomy": "Change",
    "playbook_list_available_triggers": "Understand",
    "playbook_validate": "Prove",
    "playbook_dry_run": "Prove",
    "playbook_spec_add": "Prove",
    "playbook_spec_run": "Prove",
    "playbook_spec_from_run": "Prove",
    "playbook_spec_delete": "Prove",
    "playbook_preflight": "Prove",
    "playbook_run_candidate": "Prove",
    # plans/020: the plan row is the last thing written before publish — it
    # belongs to the Ship story on the card, not Prove.
    "playbook_plan_write": "Ship",
    "playbook_plan_finish": "Ship",
    "playbook_publish": "Ship",
    "playbook_rollback": "Ship",
}


def phase_for_tool(tool_name: str) -> str:
    # Integration tools the delegate probes for real shapes are exploration.
    return _PHASE_BY_TOOL.get(tool_name, "Understand")


# Our own prompt_always tools — the calls that park the delegate on an
# approval card. Kept in sync with agent_tools.py by a drift test.
_GATED_TOOLS = frozenset({
    "playbook_set_autonomy",
    "playbook_edit_force",
    "playbook_manifest_set",
    "playbook_publish",
    "playbook_rollback",
    "playbook_run_candidate",
    "playbook_spec_delete",
})

# A gated call normally resolves in well under a second; one still pending
# after this long means the delegate is parked waiting for the owner.
_WAITING_THRESHOLD_S = 8.0

# Owner words for the gated tools — what the wait means to the person who
# has to approve it. The tool code itself must never reach the owner's eyes
# (vocabulary rule): the status message hands the model these words instead.
_GATED_TOOL_OWNER_WORDS = {
    "playbook_publish": "make the change live",
    "playbook_rollback": "roll back the live version",
    "playbook_edit_force": "force past failing specs",
    "playbook_manifest_set": "change the playbook's contract",
    "playbook_spec_delete": "delete a spec",
    "playbook_set_autonomy": "change how it runs on its own",
    "playbook_run_candidate": "test-run the draft version",
}

# plans/020: publish is plan-gated — the delegate writes the plan row itself.
_PLAN_TOOLS = (
    "playbook_plan_write",
    "playbook_plan_read",
    "playbook_plan_finish",
)


def waiting_on_owner(events: list[dict] | None,
                     now: datetime | None = None) -> str | None:
    """Tool name the delegate is parked on awaiting approval, or None.

    Derived from the event feed — never a status value, and never a code the
    model emits: the LAST event is a gated tool call with no result yet
    (`ms` unset), older than the threshold.
    """
    if not events:
        return None
    last = events[-1]
    if last.get("kind") != "tool" or last.get("ms") is not None:
        return None
    tool = str(last.get("label") or "")
    if tool not in _GATED_TOOLS:
        return None
    try:
        ts = datetime.fromisoformat(str(last.get("ts")))
    except (ValueError, TypeError):
        return None
    age = ((now or _utcnow()) - ts).total_seconds()
    return tool if age >= _WAITING_THRESHOLD_S else None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _referenced_tools(definition: dict) -> list[str]:
    """Tool names used by the playbook's tool_call steps, tree-deep."""
    try:
        d = PlaybookDef.model_validate(definition)
    except Exception:  # noqa: BLE001 — a broken definition just adds no tools
        return []

    names: list[str] = []

    def _walk(steps: list[StepDef]) -> None:
        for s in steps:
            if s.tool:
                names.append(s.tool)
            for sub in (s.then, s.else_, s.body):
                if sub:
                    _walk(sub)
            if s.branches:
                for b in s.branches:
                    _walk(b)

    _walk(d.steps)
    return sorted(set(names))


async def delegate_toolset(
    session_factory, playbook_name: str, authoring_tools: tuple[str, ...]
) -> list[str]:
    """The delegate's allowlist: authoring tools + the plan tools (publish
    is plan-gated — the delegate writes the plan row itself) + run/inspect +
    the tools the target playbook's steps reference. Never send_chat_message
    — the card is the owner-facing surface, not delegate chatter."""
    tools = (
        list(authoring_tools)
        + list(_PLAN_TOOLS)
        + ["playbook_list", "playbook_status"]
    )
    if playbook_name:
        async with session_factory() as session:
            pb = (await session.execute(
                select(Playbook).where(Playbook.name == playbook_name)
            )).scalar_one_or_none()
        if pb is not None:
            tools += _referenced_tools(pb.definition)
    seen: set[str] = set()
    out = []
    for t in tools:
        if t != "send_chat_message" and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# plans/020 phase 2: the delegate prompt is a first-class artifact — the
# vision doc's whole point. Eleven sections; the pblang reference is fetched
# just-in-time via playbook_language_reference and NEVER pasted here (the old
# prompt dragged the 12KB authoring skill into every delegation).
_PROMPT_TAIL = """\
Remember:
- The reference tool, not memory, is the source of pblang syntax.
- Done = published, or a named blocker, or a named failure — nothing vaguer.
- dry_run output is simulated — never report it as real.
- Your final text is the report the owner gets. Make it count."""


def _delegate_prompt(task: str, pb: Playbook | None) -> str:
    brief = ["## 2. Your brief", "", task.strip()]
    if pb is not None:
        brief += ["", f"Target playbook: `{pb.name}` — edit it IN PLACE by "
                  "name; never create a '-v2' copy."]
        if pb.manifest:
            brief += ["", "Its manifest — the owner's intent; your edits "
                      "must stay within it (a conflict refusal means fix "
                      "the code or ask via the gated tools):", "",
                      pb.manifest]

    sections = [
        "# Playbook delegate",
        "",
        "## 1. Who you are, and your conditions",
        "",
        "You are a focused playbook delegate: a background agent doing "
        "exactly ONE playbook authoring job with your tools. The owner "
        "never sees your working text — a progress card mirrors your tool "
        "calls, and your final text becomes your report. Your budget is "
        "about 40 tool calls and 15 minutes of wall clock — sized for one "
        "disciplined pass of the work loop plus retries, not for "
        "wandering; a breach aborts the run and the owner is told where "
        "you stopped.",
        "",
        *brief,
        "",
        "## 3. \"Done\" is an artifact, not a feeling",
        "",
        "The job ends in exactly one of three states, and your report "
        "names which:",
        "1. PUBLISHED — a version went live through playbook_publish and "
        "its machine-checked gates.",
        "2. CANDIDATE STOP — a candidate is saved and proven as far as the "
        "gates allow, and something outside your control blocks publish "
        "(an owner decision, a declined approval, a dead credential). Name "
        "the exact blocker.",
        "3. FAILED — no sound candidate. Say what you tried and where it "
        "broke.",
        "Never report \"should work\" or \"probably fine\" — unproven is "
        "state 2 or 3.",
        "",
        "## 4. The work loop",
        "",
        "Work the phases in order. The retry caps stop thrash, not effort.",
        "",
        "1. ORIENT (≤5 calls). Call playbook_language_reference FIRST and "
        "read it — exact signatures, loop kwargs, state ops, and filters "
        "live there, never in your memory; never guess syntax. For an "
        "edit/fix job also read the target: playbook_edit(name) for the "
        "ticket + manifest + code frames, playbook_status for the failing "
        "run's per-step data.",
        "2. OUTLINE, then author. Write the decomposition first, one line "
        "per step: `id -> kind -> the SINGLE operation`. Self-check: a "
        "quantifier (each/all/every) means a loop; one llm/agent step is "
        "ONE judgment on ONE thing; mechanical work goes in tool()/code(); "
        "pure transforms default to llm() with output= (typed fields, not "
        "prose); loops gather with collect=. Create with playbook_propose "
        "(pass manifest=); edit through the two-step ticket flow, copying "
        "old= snippets verbatim from the code frame.",
        "3. VALIDATE — playbook_validate reports ALL errors at once. Cap: "
        "after 3 failed validates in a row, stop patching blind — "
        "re-fetch the reference for the failing construct and re-derive "
        "the code from it.",
        "4. DRY-RUN — playbook_dry_run proves loops iterate, branches "
        "branch, templates resolve, against STUBBED tools. Copy your "
        "steps.<id>... paths from its `references` block — that block is "
        "the API; the trace's per-step `output` label is not a path. Its "
        "outputs are simulated.",
        "5. SPECS — pin behavior from recorded reality: after any real "
        "run (even a failed one) start from playbook_spec_from_run; batch "
        "new specs into ONE playbook_spec_add call. Cap: 3 failed spec "
        "runs in a row means the data path is wrong — re-derive it from "
        "dry_run's references instead of bending the spec.",
        "6. PREFLIGHT — playbook_preflight probes every tool the playbook "
        "touches. A `failed` probe (dead credential, missing tool) blocks "
        "publish — report it; `unprobeable` is common and fine.",
        "7. PROOF RUN — the publish gate wants a green test run of this "
        "exact candidate since its last edit: playbook_run_candidate "
        "(owner-approved, real side effects).",
        "8. PLAN + PUBLISH — run the checklist in section 9, then ship.",
        "",
        "## 5. The quality bar",
        "",
        "The validator's lints are the floor, not the target:",
        "- monolithic-playbook (ERROR): one delegated step hiding the "
        "whole process — decompose it.",
        "- compound-leaf / agent-does-work (warnings): treat as redesign "
        "signals, not noise.",
        "- Context economy: to process N items, loop and judge ONE per "
        "iteration — never interpolate a whole collection into one "
        "prompt.",
        "- Reference shapes: tool() → steps.<id>.result.<field>; "
        "schemaless llm()/agent() → steps.<id>._raw (there is no "
        ".output); loop() → steps.<id>.collected; code() → "
        "steps.<id>.result.",
        "- Discoverable collections (crawl/scan/traverse) are discovered "
        "at RUN TIME with a state() frontier loop — never hardcoded "
        "sibling calls; a while_ loop always sets max_iterations.",
        "",
        "## 6. Budgets and stop rules",
        "",
        "- ~40 calls / 15 min (why: one disciplined pass with retries; "
        "more usually means thrash, and the owner is better served by an "
        "honest stop). Around call 30, choose deliberately: the smallest "
        "publishable version of the brief, or a clean candidate stop.",
        "- Before ANY retry after a cap strikes: list the ~5 likeliest "
        "causes, rank them, and act on the top one — never re-run the "
        "same failing call unchanged.",
        "- Blocked on something only the owner can resolve → stop NOW and "
        "report state 2. An early honest stop beats a late confabulated "
        "finish.",
        "",
        "## 7. Actions and their weight",
        "",
        "- FREE: reads, validate, dry_run, spec_run, preflight — use "
        "freely within budget.",
        "- SIDE-EFFECTING: playbook_run and playbook_run_candidate touch "
        "the real world — only when the job needs real proof.",
        "- OWNER-DECISION: publish, rollback, edit_force, manifest_set, "
        "spec_delete, run_candidate, set_autonomy raise a real approval "
        "card in the owner's chat. Call them and WAIT — the pause is the "
        "owner deciding. A decline is an answer: respect it in your "
        "report; never work around a refusal or a decline.",
        "",
        "## 8. Worked shapes",
        "",
        "A minimal real playbook (loop + one judgment per item + typed "
        "collect):",
        "",
        "```python",
        "playbook(name='digest-open-prs', description='Digest PRs needing "
        "review',",
        "    when_to_use='Owner asks what PRs are waiting on them')",
        "",
        "fetch = tool('github_list_prs', state='open')",
        "scan = loop(over='{{ steps.fetch.result.items }}', "
        "item_name='pr', concurrency=4,",
        "    body=[(judge := llm('Does THIS ONE PR need the owner? "
        "{{ pr }}',",
        "        output={'needs_review': 'bool', 'title': 'str'}))],",
        "    collect='{{ steps.judge }}')",
        "digest = llm(\"Short digest of: {{ steps.scan.collected | "
        "selectattr('needs_review') | list }}\",",
        "    output={'digest': 'str'})",
        "```",
        "",
        "BAD → GOOD: `agent('Check all open PRs, decide which need "
        "review, and write a digest')` is the whole task hiding in one "
        "step — monolithic-playbook, invisible loop, nothing inspectable. "
        "The shape above is the same job decomposed: each step visible, "
        "typed data between them.",
        "",
        "A good final report:",
        "\"PUBLISHED v3 of digest-open-prs. Added the per-PR judgment "
        "loop and a typed digest step. validate clean, 4/4 specs pass, "
        "dry-run traces the loop over stubbed PRs (simulated), preflight "
        "ok, test run green. Nothing needs you.\"",
        "",
        "## 9. Pre-publish checklist",
        "",
        "Immediately before publishing, confirm every line:",
        "1. playbook_validate is clean on the candidate.",
        "2. Specs pass, and at least one spec pins the changed behavior.",
        "3. A green test run of THIS exact candidate exists since its "
        "last edit (playbook_run_candidate).",
        "4. preflight shows no `failed` tools (external-service "
        "playbooks).",
        "5. The plan row is written: playbook_plan_write — the "
        "owner-readable intent of THIS change; publish requires its "
        "plan_id.",
        "6. The manifest is still true (or was updated through the gated "
        "tools).",
        "Then playbook_publish(name, plan_id=..., explanation=...) — the "
        "explanation in owner words, not tool words. After it resolves, "
        "playbook_plan_finish.",
        "",
        "## 10. Your final report",
        "",
        "At most 6 sentences, owner words, no tool names. It must name "
        "the end state (published vN / candidate stop + blocker / failed "
        "+ why), what changed, and what is proven vs. simulated — dry_run "
        "output is simulated and is never reported as a real result.",
        "",
        "## 11. Before you finish",
        "",
        _PROMPT_TAIL,
    ]
    return "\n".join(sections)


# plans/020 phase 2: the terminal event stream is the dojoP bench's grading
# surface — each tool event also carries the call `args` (values capped,
# secret-looking keys redacted) and an `ok` verdict on completion.
_ARG_VALUE_CAP = 200
_SECRET_KEY_RE = re.compile(
    r"token|secret|password|authorization|api_?key|credential", re.I
)


def _scrub_args(raw: Any) -> dict | str | None:
    """Call args made safe to persist: JSON-decoded, values capped, keys
    that look like secrets redacted. Never raises — args are best-effort."""
    try:
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = json.loads(raw) if raw.strip() else {}
        if not isinstance(raw, dict):
            return str(raw)[:_ARG_VALUE_CAP]
        out: dict[str, Any] = {}
        for k, v in raw.items():
            if _SECRET_KEY_RE.search(str(k)):
                out[str(k)] = "•••"
                continue
            if isinstance(v, (int, float, bool)) or v is None:
                out[str(k)] = v
                continue
            s = v if isinstance(v, str) else json.dumps(v, default=str)
            out[str(k)] = s[:_ARG_VALUE_CAP] + "…" if len(s) > _ARG_VALUE_CAP else s
        return out
    except Exception:  # noqa: BLE001 — never let arg capture kill the feed
        return None


def _result_ok(content: Any) -> bool:
    """Did the tool call succeed? Plugin tools report failure as JSON with
    an `error` key (or an Error… string); anything else counts as ok."""
    try:
        if isinstance(content, dict):
            return not content.get("error")
        if isinstance(content, str):
            s = content.strip()
            if s.startswith("{"):
                d = json.loads(s)
                return not (isinstance(d, dict) and d.get("error"))
            return not s.lower().startswith("error")
    except Exception:  # noqa: BLE001
        pass
    return True


class _EventFeed:
    """Maps the pydantic-ai event stream to card events, throttled to the DB.

    Duck-typed on event attribute shapes (no pydantic_ai import — the plugin
    ships SDK-only). Tool timing keys on tool_call_id.
    """

    def __init__(self, session_factory, delegation_id: uuid.UUID) -> None:
        self._sf = session_factory
        self._id = delegation_id
        self.events: list[dict] = []
        self.steps_used = 0
        self._t0: dict[str, float] = {}
        self._last_flush = 0.0
        self._dirty = False

    def _append(self, kind: str, label: str, detail: str = "",
                phase: str | None = None, ms: int | None = None,
                args: dict | str | None = None) -> None:
        ev: dict[str, Any] = {
            "ts": _utcnow().isoformat(),
            "phase": phase or "Understand",
            "kind": kind,
            "label": label[:200],
            "detail": detail[:300],
            "ms": ms,
        }
        if args is not None:
            ev["args"] = args
        self.events.append(ev)
        self._dirty = True

    async def handle(self, _ctx: Any, stream: Any) -> None:
        """The 049 event_stream_handler: consume one model-request stream."""
        loop = asyncio.get_running_loop()
        async for ev in stream:
            try:
                self._map_event(ev, loop)
            except Exception:  # noqa: BLE001 — the feed must never kill the run
                log.exception("delegation: event mapping failed")
            await self.maybe_flush()

    def _map_event(self, ev: Any, loop: asyncio.AbstractEventLoop) -> None:
        part = getattr(ev, "part", None)
        # A tool call starting (FunctionToolCallEvent has .part ToolCallPart).
        tool_name = getattr(part, "tool_name", None) or getattr(ev, "tool_name", None)
        call_id = (
            getattr(part, "tool_call_id", None)
            or getattr(ev, "tool_call_id", None)
        )
        if type(ev).__name__ == "FunctionToolCallEvent" and tool_name:
            self._t0[str(call_id)] = loop.time()
            self.steps_used += 1
            self._append("tool", tool_name, phase=phase_for_tool(tool_name),
                         args=_scrub_args(getattr(part, "args", None)))
            return
        # A tool result. pydantic-ai <2 carried the ToolReturnPart as
        # .result; >=2 (QA runs 2.35) carries it as .part. Accept both.
        result = getattr(ev, "result", None)
        if type(ev).__name__ == "FunctionToolResultEvent" and result is None:
            result = part
        if type(ev).__name__ == "FunctionToolResultEvent" and result is not None:
            rname = getattr(result, "tool_name", "") or ""
            rid = str(getattr(result, "tool_call_id", "") or "")
            t0 = self._t0.pop(rid, None)
            ms = int((loop.time() - t0) * 1000) if t0 is not None else None
            content = getattr(result, "content", "")
            detail = content if isinstance(content, str) else json.dumps(
                content, default=str
            )
            # Find the matching started event and complete it in place, so
            # the card shows one line per call, not call+result pairs.
            for e in reversed(self.events):
                if e["kind"] == "tool" and e["label"] == rname and e["ms"] is None:
                    e["ms"] = ms
                    e["detail"] = detail[:300]
                    e["ok"] = _result_ok(content)
                    self._dirty = True
                    return
            self._append("tool", rname or "tool", detail=detail,
                         phase=phase_for_tool(rname), ms=ms)
            self.events[-1]["ok"] = _result_ok(content)
            return
        # Assistant text between tool calls — first line only, dim on the card.
        if type(ev).__name__ == "PartStartEvent" and part is not None:
            text = getattr(part, "content", None)
            if isinstance(text, str) and text.strip() and not tool_name:
                self._append("thought", text.strip().splitlines()[0])

    async def maybe_flush(self, force: bool = False) -> None:
        loop = asyncio.get_running_loop()
        if not self._dirty:
            return
        if not force and (loop.time() - self._last_flush) < _FLUSH_INTERVAL_S:
            return
        self._last_flush = loop.time()
        self._dirty = False
        async with self._sf() as session:
            row = await session.get(PlaybookDelegation, self._id)
            if row is not None:
                row.events = list(self.events)
                row.steps_used = self.steps_used
                await session.commit()


def current_phase(events: list[dict] | None) -> str:
    for e in reversed(events or []):
        if e.get("kind") == "tool":
            return e.get("phase") or "Understand"
    return "Understand"


async def _finish(session_factory, delegation_id: uuid.UUID, *, status: str,
                  result: str, feed: _EventFeed) -> None:
    await feed.maybe_flush(force=True)
    async with session_factory() as session:
        row = await session.get(PlaybookDelegation, delegation_id)
        if row is None:
            return
        row.status = status
        row.result = result
        row.events = list(feed.events)
        row.steps_used = feed.steps_used
        row.finished_at = _utcnow()
        await session.commit()


async def _drive_delegation(
    ctx: Any,
    session_factory,
    delegation_id: uuid.UUID,
    prompt: str,
    tools: list[str],
    conversation_id: Any,
) -> None:
    feed = _EventFeed(session_factory, delegation_id)
    # Expose the live feed for the card route — DB flushes are throttled,
    # but a poll may read the in-memory feed for freshness.
    _LIVE_FEEDS[delegation_id] = feed
    try:
        # plans/020: luna 098 collapsed conversation states to
        # planning/building. The delegate always runs as "building" — its
        # whole job is building, and the publish-class tools it needs
        # declare modes=["planning","building"] or are plan-gated anyway.
        # Containment comes from the explicit `tools` allowlist plus the
        # machine-checked publish gates, not from the spawning chat's state.
        result, _usage = await ctx.agent.run_turn(
            prompt,
            tools=tools,
            memory_write=False,
            memory_read=False,
            conversation_id=conversation_id,
            max_turns=_MAX_TURNS,
            timeout_s=_TIMEOUT_S,
            event_stream_handler=feed.handle,
            conversation_state="building",
        )

        if isinstance(result, dict) and result.get("_aborted"):
            last = feed.events[-1]["label"] if feed.events else "the start"
            reason = str(result.get("_aborted"))
            kind = "step budget" if "request" in reason or "usage" in reason \
                else "time budget"
            await _finish(
                session_factory, delegation_id, status="needs_owner",
                result=(
                    f"Stopped at the {kind} after {feed.steps_used} tool "
                    f"calls (last: {last}). The job is bigger than one "
                    "delegation — needs your call on how to proceed."
                ),
                feed=feed,
            )
            return
        if isinstance(result, dict) and result.get("error"):
            await _finish(
                session_factory, delegation_id, status="failed",
                result=str(result.get("error"))[:1000], feed=feed,
            )
            return

        text = result if isinstance(result, str) else json.dumps(result, default=str)
        await _finish(session_factory, delegation_id, status="done",
                      result=text.strip() or "Done.", feed=feed)
    except Exception as e:  # noqa: BLE001 — a crash must land on the row
        log.exception("delegation %s crashed", delegation_id)
        try:
            await _finish(session_factory, delegation_id, status="failed",
                          result=f"Delegate crashed: {e}", feed=feed)
        except Exception:  # noqa: BLE001
            log.exception("delegation %s: could not record crash", delegation_id)
    finally:
        _LIVE_FEEDS.pop(delegation_id, None)


# Live in-process feeds, keyed by delegation id — the card route reads these
# first so 1s-throttled DB flushes never make the card feel stale.
_LIVE_FEEDS: dict[uuid.UUID, _EventFeed] = {}


def _delegation_payload(row: PlaybookDelegation, *, for_status_tool: bool) -> dict:
    # While running, prefer the live in-process feed over the row: the DB
    # flush is throttled, and a delegate that parks on an approval right
    # after a gated call may never flush that last event — the row would
    # then hide the very wait we need to surface.
    feed = _LIVE_FEEDS.get(row.id) if row.status == "running" else None
    events = feed.events if feed is not None else (row.events or [])
    payload: dict[str, Any] = {
        "delegation_id": str(row.id),
        "status": row.status,
        "playbook": row.playbook or None,
        "steps_used": feed.steps_used if feed is not None else row.steps_used,
    }
    if row.status == "running":
        waiting = waiting_on_owner(events)
        if waiting is not None:
            words = _GATED_TOOL_OWNER_WORDS.get(
                waiting, waiting.replace("_", " "))
            payload["waiting_for_approval"] = waiting
            payload["message"] = (
                "The delegate is PAUSED waiting for the owner's approval to "
                f"{words} — an approval card is in the chat. Tell the owner "
                "(in those words, never the tool name) that their approval "
                "is needed, then END YOUR TURN."
            )
        else:
            payload["message"] = (
                "The build is in progress — not created or published yet; "
                "saying it is underway is correct, saying it is created/ready/"
                "live is false. A progress card in the chat tracks it live. "
                "Reply with ONE sentence that names the playbook and the "
                "change underway (e.g. \"the crm-import playbook build is "
                "underway — the card below tracks it\"), then END YOUR TURN. "
                "Do not poll playbook_agent_status unless the owner asks "
                "later."
            )
    elif row.status in ("done", "failed", "needs_owner"):
        if row.result:
            payload["report"] = row.result[:_RESULT_CAP]
        if row.status == "failed":
            payload["message"] = "The delegation FAILED — the report says why."
        elif row.status == "needs_owner":
            payload["message"] = (
                "The delegate stopped and needs the owner's decision — relay "
                "the report."
            )
    if for_status_tool and events:
        payload["recent_events"] = [
            {k: e.get(k) for k in ("phase", "kind", "label", "ms")}
            for e in events[-5:]
        ]
    return payload


def build_delegation_tools(ctx: Any, session_factory, authoring_tools: tuple[str, ...]):
    """(ToolDef, handler) pairs for the delegation tools — plans/013 phase 1,
    reinstated by plans/020."""

    async def _playbook_agent(*, task: str, playbook: str = "",
                              wait_seconds: float | None = None) -> str:
        if not task.strip():
            return json.dumps({"error": "task is empty"})
        wait = _WAIT_DEFAULT if wait_seconds is None else float(wait_seconds)
        wait = max(0.0, min(wait, _WAIT_MAX))

        pb: Playbook | None = None
        if playbook:
            async with session_factory() as session:
                pb = (await session.execute(
                    select(Playbook).where(Playbook.name == playbook)
                )).scalar_one_or_none()
            if pb is None:
                return json.dumps({
                    "error": f"Playbook '{playbook}' not found. Tell the "
                    "owner it doesn't exist and offer to create it — only "
                    "delegate a from-scratch job (omit `playbook`) after "
                    "they say yes."
                })

        conversation_id = ctx.current_conversation_id
        row = PlaybookDelegation(
            task=task,
            playbook=playbook,
            status="running",
            card_token=secrets.token_urlsafe(24),
            conversation_id=conversation_id,
        )
        async with session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)

        # The live progress card, posted as its own timeline row (luna 056).
        # Feature-detected — on older cores the tools still work, the owner
        # just gets no card. Never let card trouble kill the job.
        post_card = getattr(ctx, "post_chat_card", None)
        if post_card is not None:
            try:
                from . import PlaybooksPlugin
                from .card import render_delegation_card

                html = render_delegation_card(
                    str(row.id), row.card_token, playbook,
                    str(getattr(PlaybooksPlugin.manifest, "version", "0")),
                )
                message_id = await post_card(
                    html, conversation_id=conversation_id,
                )
                if message_id:
                    async with session_factory() as session:
                        fresh = await session.get(PlaybookDelegation, row.id)
                        if fresh is not None:
                            fresh.card_message_id = str(message_id)
                            await session.commit()
            except Exception:  # noqa: BLE001
                log.exception("delegation %s: card post failed", row.id)

        tools = await delegate_toolset(session_factory, playbook, authoring_tools)
        prompt = _delegate_prompt(task, pb)

        task_obj = asyncio.create_task(_drive_delegation(
            ctx, session_factory, row.id, prompt, tools, conversation_id,
        ))
        _TASKS[row.id] = task_obj
        task_obj.add_done_callback(lambda _t, _id=row.id: _TASKS.pop(_id, None))

        if wait > 0:
            live = _TASKS.get(row.id)
            if live is not None:
                await asyncio.wait([live], timeout=wait)
        async with session_factory() as session:
            fresh = await session.get(PlaybookDelegation, row.id)
        return json.dumps(_delegation_payload(fresh, for_status_tool=False))

    async def _playbook_agent_status(*, delegation_id: str) -> str:
        try:
            did = uuid.UUID(delegation_id)
        except ValueError:
            return json.dumps({"error": "invalid delegation_id"})
        async with session_factory() as session:
            row = await session.get(PlaybookDelegation, did)
        if row is None:
            return json.dumps({"error": "Delegation not found"})
        return json.dumps(_delegation_payload(row, for_status_tool=True))

    return [
        (
            ToolDef(
                name="playbook_agent",
                # luna 098: the only states are planning/building.
                modes=["planning", "building"],
                artifact_ref="playbook:{playbook}",
                # chat_only: a delegate (or any headless turn) must never
                # spawn delegations — same recursion guard as playbook_run.
                chat_only=True,
                timeout_seconds=120,
                description=(
                    "Delegate a playbook authoring job (create, fix, edit, "
                    "add specs) to a focused background agent. It works "
                    "through the full loop (read, edit, validate, dry-run, "
                    "specs, plan, publish) in its own context; a live "
                    "progress card appears in the chat. Returns within "
                    "wait_seconds (default 25): either the finished report "
                    "or status 'running' — then tell the owner the card "
                    "tracks the work and END your turn; never poll."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "The job, phrased with goal + acceptance, "
                                "e.g. 'Fix the phone format in "
                                "candidate-intake: normalize to E.164; all "
                                "specs must pass; publish when green.'"
                            ),
                        },
                        "playbook": {
                            "type": "string",
                            "description": (
                                "Target playbook name for edit/fix jobs "
                                "(omit when creating from scratch)."
                            ),
                        },
                        "wait_seconds": {
                            "type": "number",
                            "description": "0-90, default 25. 0 = return immediately.",
                        },
                    },
                    "required": ["task"],
                },
                policy="auto_approve",
                risk_level="medium",
            ),
            _playbook_agent,
        ),
        (
            ToolDef(
                name="playbook_agent_status",
                modes=["planning", "building"],
                timeout_seconds=30,
                description=(
                    "Check a playbook delegation started by playbook_agent. "
                    "Use ONLY when the owner asks how it is going — the "
                    "progress card already tracks it live."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "delegation_id": {"type": "string"},
                    },
                    "required": ["delegation_id"],
                },
                policy="auto_approve",
                risk_level="low",
            ),
            _playbook_agent_status,
        ),
    ]


async def sweep_orphaned_delegations(session_factory) -> int:
    """Mark 'running' rows with no live task as failed (restart hygiene) —
    same convergence rule as the runner's orphan sweep."""
    swept = 0
    async with session_factory() as session:
        rows = (await session.execute(
            select(PlaybookDelegation).where(PlaybookDelegation.status == "running")
        )).scalars().all()
        for row in rows:
            if row.id in _TASKS:
                continue
            row.status = "failed"
            row.result = (
                "Luna restarted while this delegation was running; it did "
                "not finish. Start it again if still wanted."
            )
            row.finished_at = _utcnow()
            swept += 1
        if swept:
            await session.commit()
    if swept:
        log.info("playbooks: swept %d orphaned delegation(s)", swept)
    return swept
