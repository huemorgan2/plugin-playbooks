"""plans/028 — wake-on-completion for playbook runs.

Agents no longer poll `playbook_status` for slow runs: `playbook_run` stamps
`wake_on_complete` on the run when its wait window lapses (or on
fire-and-forget), and this service delivers the outcome when the run
finishes:

- flagged runs → a muted MOMENT to the originating conversation (ops chat
  fallback): a real agent turn that reads the result and reports/acts. If
  the originating turn is still alive, core's queue-if-busy injects the
  result into it via the inbox instead of starting a second turn.
- other live background runs (webhook/scheduler triggers) → an AWARENESS
  row in the ops chat: the events inbox learns the run happened, zero
  tokens, no turn. Failures of those runs already get their moment from
  FixProposalService — never a second one from here.
- test runs and subtask runs are silent (interactive grading / the parent
  run reports).

Mirrors FixProposalService: bus subscriber, background=True, spawned tasks
kept referenced, every failure swallowed — a wake error must never damage
the completion path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    # sqlite returns naive datetimes; stored values are UTC
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

from .models import PlaybookStepRun, PlaybookWatch
from .publish import ops_conversation_id

log = logging.getLogger(__name__)

# Containment for the wake turn — plugin-tasks' resume defaults.
_WAKE_MAX_TURNS = 12
_WAKE_TOKEN_BUDGET = 200_000
_WAKE_TIMEOUT_S = 900.0
# Step outputs are inlined into the moment body up to this cap; beyond it the
# agent is steered to playbook_status for the full trace.
_OUTPUTS_CAP = 4000


class RunCompletionWake:
    """Subscribes to `playbook.run.completed` and wakes/notifies the agent."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        events: Any,
        ctx: Any = None,
    ) -> None:
        self._sf = session_factory
        self._events = events
        self._ctx = ctx
        self._unsub: Any = None
        self._tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        self._unsub = self._events.subscribe(
            "playbook.run.completed", self._on_completed, background=True,
        )
        log.info("run_wake.started")

    def stop(self) -> None:
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:  # noqa: BLE001
                pass
            self._unsub = None
        for t in self._tasks:
            t.cancel()

    async def _on_completed(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("is_test") or payload.get("parent_run_id"):
            return
        task = asyncio.create_task(
            self._deliver(payload), name="playbook-run-wake",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, payload: dict[str, Any]) -> None:
        try:
            await self._deliver_inner(payload)
        except Exception:  # noqa: BLE001 — never let a wake error escape
            log.exception("run_wake.failed payload=%s", payload)

    async def _deliver_inner(self, payload: dict[str, Any]) -> None:
        ctx = self._ctx
        send = getattr(ctx, "send_muted_message", None) if ctx else None
        if send is None:
            return  # old core or headless test ctx: nothing to deliver with

        # 0.46.0 (plans/029): watchers first — any trigger. The pass consumes
        # watches even when their moment is suppressed by a path that already
        # delivers to the same conversation (dedupe rules inside).
        await self._watch_pass(send, payload)

        if payload.get("wake_on_complete"):
            await self._wake_moment(send, payload)
            return

        trigger = payload.get("trigger") or ""
        if trigger in ("agent", "agent-candidate"):
            # un-flagged agent run: the tool already returned the result
            # inline — a wake here would double-report.
            return
        await self._awareness_note(send, payload)

    async def _watch_pass(self, send: Any, payload: dict[str, Any]) -> None:
        """plans/029: deliver one-shot `playbook_watch` promises.

        Per (run, conversation) at most ONE moment ever fires: a watcher
        conversation already served by another path — the launcher's 028
        moment, an agent run's inline tool result, or the fix-proposal
        service's ops failure moment — gets its watch consumed silently.
        """
        pb_id = payload.get("playbook_id")
        if not pb_id:
            return
        try:
            pb_uuid = uuid.UUID(str(pb_id))
        except ValueError:
            return
        now = _utcnow()
        async with self._sf() as session:
            rows = (await session.execute(
                select(PlaybookWatch).where(
                    PlaybookWatch.playbook_id == pb_uuid,
                    PlaybookWatch.consumed_at.is_(None),
                )
            )).scalars().all()
            expired = [w for w in rows if _aware(w.expires_at) <= now]
            for w in expired:
                await session.delete(w)
            if expired:
                await session.commit()
            watches = [w for w in rows if _aware(w.expires_at) > now]
        if not watches:
            return

        origin = payload.get("conversation_id")
        failed = (payload.get("status") or "") == "failed"
        trigger = payload.get("trigger") or ""
        # Failure moments in the ops chat belong to FixProposalService.
        ops = await ops_conversation_id(self._ctx) if failed else None

        for w in watches:
            if not await self._claim_watch(w.id):
                continue  # another completion consumed it first
            conv = w.conversation_id
            silent = (
                # launcher == watcher: the 028 launcher moment covers it
                (payload.get("wake_on_complete") and origin
                 and str(conv) == str(origin))
                # agent run, same conversation: the tool result was inline
                or (trigger in ("agent", "agent-candidate") and origin
                    and str(conv) == str(origin))
                # failed background run, watcher is the ops chat: the
                # fix-proposal moment owns it
                or (failed and ops is not None and str(conv) == str(ops))
            )
            if silent:
                log.info("run_wake.watch_consumed_silently watch=%s", w.id)
                continue
            await self._watch_moment(send, payload, conv, w.note)

    async def _claim_watch(self, watch_id: Any) -> bool:
        from sqlalchemy import update

        async with self._sf() as session:
            res = await session.execute(
                update(PlaybookWatch)
                .where(
                    PlaybookWatch.id == watch_id,
                    PlaybookWatch.consumed_at.is_(None),
                )
                .values(consumed_at=_utcnow())
            )
            await session.commit()
            return bool(res.rowcount)

    async def _watch_moment(
        self, send: Any, payload: dict[str, Any], conv: Any, note: str | None,
    ) -> None:
        name = payload.get("playbook_name") or "?"
        run_id = payload.get("run_id") or "?"
        status = payload.get("status") or "?"
        lines = [
            f"The '{name}' playbook you asked to be woken about has "
            f"finished a run with status '{status}' "
            f"(trigger: {payload.get('trigger') or '?'}).",
            f"Run: {run_id}",
        ]
        if note:
            lines.append(f"Your note when you set the watch: {note}")
        if status == "failed":
            lines.append(f"Error: {payload.get('error') or 'not recorded'}")
            lines.append("")
            lines.append(
                "Report the failure to the owner honestly — do NOT fabricate "
                "results. playbook_status(run_id) shows the failing trace."
            )
        else:
            outputs = await self._collect_outputs(run_id)
            if outputs:
                lines.append("")
                lines.append(f"Step outputs:\n{outputs}")
            lines.append("")
            lines.append(
                "Continue what you were waiting on. This was a one-shot "
                "watch — set playbook_watch again if you need the next run "
                "too."
            )
        try:
            await send(
                f"Watched playbook finished: {name}",
                "\n".join(lines),
                channel="moment",
                respond=True,
                conversation_id=conv,
                source="playbooks",
                tools="all",
                max_turns=_WAKE_MAX_TURNS,
                token_budget=_WAKE_TOKEN_BUDGET,
                timeout_s=_WAKE_TIMEOUT_S,
            )
            log.info("run_wake.watch_moment run=%s conv=%s", run_id, conv)
        except Exception:  # noqa: BLE001
            log.exception("run_wake.watch_moment_failed run=%s", run_id)

    async def _wake_moment(self, send: Any, payload: dict[str, Any]) -> None:
        name = payload.get("playbook_name") or "?"
        run_id = payload.get("run_id") or "?"
        status = payload.get("status") or "?"
        duration_s = int((payload.get("duration_ms") or 0) / 1000)

        conv = None
        if payload.get("conversation_id"):
            try:
                conv = uuid.UUID(str(payload["conversation_id"]))
            except ValueError:
                conv = None
        if conv is None:
            conv = await ops_conversation_id(self._ctx)
        if conv is None:
            log.warning("run_wake.unroutable run=%s", run_id)
            return

        lines = [
            f"The '{name}' playbook run you started earlier has finished "
            f"with status '{status}' after {duration_s}s.",
            f"Run: {run_id}",
        ]
        if status == "failed":
            lines.append(f"Error: {payload.get('error') or 'not recorded'}")
            lines.append("")
            lines.append(
                "Report the failure to the owner honestly — do NOT fabricate "
                "results. playbook_status(run_id) shows the failing trace."
            )
        else:
            outputs = await self._collect_outputs(run_id)
            if outputs:
                lines.append("")
                lines.append(f"Step outputs:\n{outputs}")
            else:
                lines.append(
                    "The run produced no step outputs — check "
                    "playbook_status(run_id) before reporting."
                )
            lines.append("")
            lines.append(
                "Report the outcome to the owner now, continuing what the "
                "original request asked for."
            )
        try:
            await send(
                f"Playbook finished: {name}",
                "\n".join(lines),
                channel="moment",
                respond=True,
                conversation_id=conv,
                source="playbooks",
                tools="all",
                max_turns=_WAKE_MAX_TURNS,
                token_budget=_WAKE_TOKEN_BUDGET,
                timeout_s=_WAKE_TIMEOUT_S,
            )
            log.info("run_wake.moment run=%s status=%s", run_id, status)
        except Exception:  # noqa: BLE001
            log.exception("run_wake.moment_failed run=%s", run_id)

    async def _awareness_note(self, send: Any, payload: dict[str, Any]) -> None:
        name = payload.get("playbook_name") or "?"
        run_id = payload.get("run_id") or "?"
        status = payload.get("status") or "?"
        conv = await ops_conversation_id(self._ctx)
        if conv is None:
            return
        body = (
            f"Background run of '{name}' finished: {status} "
            f"(trigger: {payload.get('trigger') or '?'}, run {run_id})."
        )
        if status == "failed":
            body += f" Error: {payload.get('error') or 'not recorded'}"
        try:
            await send(
                f"Playbook run {status}: {name}",
                body,
                channel="awareness",
                respond=False,
                conversation_id=conv,
                source="playbooks",
            )
        except Exception:  # noqa: BLE001
            log.exception("run_wake.awareness_failed run=%s", run_id)

    async def _collect_outputs(self, run_id: Any) -> str | None:
        if isinstance(run_id, str):
            try:
                run_id = uuid.UUID(run_id)
            except ValueError:
                return None
        async with self._sf() as session:
            steps = (await session.execute(
                select(PlaybookStepRun).where(PlaybookStepRun.run_id == run_id)
            )).scalars().all()
        results = {s.step_id: s.outputs for s in steps if s.outputs}
        if not results:
            return None
        text = json.dumps(results, indent=2, default=str)
        if len(text) > _OUTPUTS_CAP:
            text = text[:_OUTPUTS_CAP] + (
                "\n... (truncated — playbook_status(run_id) has the full trace)"
            )
        return text


__all__ = ["RunCompletionWake"]
