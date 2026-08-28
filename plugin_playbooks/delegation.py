"""Playbook delegation — plans/013.

`playbook_agent(task, ...)` hands a playbook authoring job to a focused
background delegate: one headless agent turn (``ctx.agent.run_turn``) whose
context carries the authoring skill + the task + the target playbook's
manifest — and nothing of the owner's conversation. The main chat keeps ONE
tool call and ONE ~1KB result; the delegate's progress is surfaced by the
chat card (phase 2), fed from the ``events`` list this module records.

Core seams used (all shipped, no core changes):
- luna 046/phase03: an explicit ``tools=`` allowlist bypasses skill-gating,
  so the delegate gets the gated authoring tools directly.
- luna 049: ``max_turns`` (hard step budget — breach returns ``{"_aborted"}``),
  ``timeout_s``, and ``event_stream_handler`` (the live event feed).
- luna 0.40.003: headless tools run the same dispatch gate as chat, so
  ``prompt_always`` tools (promote, spec_delete) still raise approval cards.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
    "playbook_promote": "Ship",
    "playbook_rollback": "Ship",
}


def phase_for_tool(tool_name: str) -> str:
    # Integration tools the delegate probes for real shapes are exploration.
    return _PHASE_BY_TOOL.get(tool_name, "Understand")


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
    """The delegate's allowlist: authoring tools + run/inspect + the tools
    the target playbook's steps reference. Never send_chat_message — the
    card is the owner-facing surface, not delegate chatter."""
    tools = list(authoring_tools) + ["playbook_list", "playbook_status"]
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


def _delegate_prompt(skill_body: str, task: str, pb: Playbook | None) -> str:
    parts = [
        "You are a focused playbook delegate: a background agent doing ONE "
        "playbook authoring job. Work with your tools until the job meets "
        "its acceptance, then stop.",
        "",
        "Rules:",
        "- The owner does NOT see your text; a progress card tracks your "
        "tool calls. Your FINAL text is your report: 2-6 plain sentences — "
        "what changed, what is proven (specs/dry-runs), what (if anything) "
        "needs the owner.",
        "- Follow the authoring loop: read → edit → validate → dry_run → "
        "specs → promote. A candidate is NOT done until promoted.",
        "- Approval-gated tools (promote, spec_delete) show the owner a "
        "card and may take a while — call them and wait; never work around "
        "a refusal.",
        "- You have a hard budget of about 40 tool calls. If the job is "
        "too big, stop early and report exactly where you stopped and why.",
        "",
        "## The authoring skill",
        skill_body,
    ]
    if pb is not None:
        parts += ["", f"## Target playbook: {pb.name}"]
        if pb.manifest:
            parts += ["### Its manifest (your edits must stay within it)",
                      pb.manifest]
    parts += ["", "## Your job", task]
    return "\n".join(parts)


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
                phase: str | None = None, ms: int | None = None) -> None:
        self.events.append({
            "ts": _utcnow().isoformat(),
            "phase": phase or "Understand",
            "kind": kind,
            "label": label[:200],
            "detail": detail[:300],
            "ms": ms,
        })
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
            self._append("tool", tool_name, phase=phase_for_tool(tool_name))
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
                    self._dirty = True
                    return
            self._append("tool", rname or "tool", detail=detail,
                         phase=phase_for_tool(rname), ms=ms)
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
    # Expose the live feed for the card route (phase 2) — DB flushes are
    # throttled, but a poll may read the in-memory feed for freshness.
    _LIVE_FEEDS[delegation_id] = feed
    try:
        try:
            result, _usage = await ctx.agent.run_turn(
                prompt,
                tools=tools,
                memory_write=False,
                memory_read=False,
                conversation_id=conversation_id,
                max_turns=_MAX_TURNS,
                timeout_s=_TIMEOUT_S,
                event_stream_handler=feed.handle,
            )
        except TypeError:
            # Older core without the 049 containment kwargs: run uncontained,
            # say so on the card instead of silently pretending.
            feed._append("thought", "budget unenforced (older Luna core)")
            result, _usage = await ctx.agent.run_turn(
                prompt,
                tools=tools,
                memory_write=False,
                conversation_id=conversation_id,
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
    payload: dict[str, Any] = {
        "delegation_id": str(row.id),
        "status": row.status,
        "playbook": row.playbook or None,
        "steps_used": row.steps_used,
    }
    if row.status == "running":
        payload["message"] = (
            "The delegate is working in the background; a progress card in "
            "the chat tracks it live. Tell the owner the card shows the "
            "progress, then END YOUR TURN. Do not poll playbook_agent_status "
            "unless the owner asks later."
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
    if for_status_tool and row.events:
        payload["recent_events"] = [
            {k: e.get(k) for k in ("phase", "kind", "label", "ms")}
            for e in row.events[-5:]
        ]
    return payload


def build_delegation_tools(ctx: Any, session_factory, authoring_tools: tuple[str, ...]):
    """(ToolDef, handler) pairs for the delegation tools — plans/013 phase 1."""
    from . import _AUTHORING_SKILL_BODY

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
                    "error": f"Playbook '{playbook}' not found. Pass an "
                    "existing name, or omit `playbook` for a from-scratch job."
                })

        conversation_id = getattr(ctx, "current_conversation_id", None)
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

        # Phase 2: the live progress card, posted as its own timeline row
        # (luna 056). Feature-detected — on older cores the tools still work,
        # the owner just gets no card. Never let card trouble kill the job.
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
        prompt = _delegate_prompt(_AUTHORING_SKILL_BODY, task, pb)

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
                # chat_only: a delegate (or any headless turn) must never
                # spawn delegations — same recursion guard as playbook_run.
                chat_only=True,
                timeout_seconds=120,
                description=(
                    "Delegate a playbook authoring job (create, fix, edit, "
                    "add specs) to a focused background agent. It works "
                    "through the full loop (read, edit, validate, dry-run, "
                    "specs, promote) in its own context; a live progress "
                    "card appears in the chat. Returns within wait_seconds "
                    "(default 25): either the finished report or status "
                    "'running' — then tell the owner the card tracks the "
                    "work and END your turn; never poll."
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
                                "specs must pass; promote when green.'"
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
