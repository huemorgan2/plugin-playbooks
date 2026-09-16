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

from .models import FAILED_RUN_STATUSES, PlaybookRun, PlaybookStepRun, PlaybookWatch
from .publish import ops_conversation_id

log = logging.getLogger(__name__)

# Containment for the wake turn — plugin-tasks' resume defaults (luna 0.92.049,
# plans/035-fix18fails P1.5). The token budget is pydantic-ai's CUMULATIVE
# input+output meter across the turn's requests: it caps the NUMBER of model
# round-trips, not context size. The old 200k on a ~60k-token conversation
# allowed three requests and cut the third — the wake turn that was reading a
# 100k-char run result died as `aborted: token_budget` and its work vanished.
# Per-request context is bounded by core; `timeout_s` is the real limiter.
_WAKE_MAX_TURNS = 20
_WAKE_TOKEN_BUDGET = 1_500_000
_WAKE_TIMEOUT_S = 900.0
# plans/035 P1.5: a failure moment carries the traceback up to this many chars.
_TRACEBACK_CAP = 1500
_STEP_INPUTS_CAP = 600
# The instruction that closes every wake turn that reports a FAILED run: the
# owner's original request is still the job — the old text ("Report the
# failure … playbook_status shows the trace") ended the turn at the report.
_CONTINUE_AFTER_FAILURE = (
    "Then continue what the original request asked for — fix and rerun if the "
    "request said so; report honestly what you did and what you did not."
)
# Step outputs are inlined into the moment body up to this cap; beyond it the
# agent is steered to playbook_status for the full trace.
_OUTPUTS_CAP = 4000

# plans/032 phase 08: a `timed_out_unknown` run (docs/v2.md §6) reads as a
# failure everywhere — plus this sentence, so nobody re-runs blind.
_OUTCOME_UNKNOWN_LINE = (
    "Outcome unknown — an effect was in flight when the process died and "
    "its result was never recorded. Do NOT assume it did or did not "
    "happen; check the target system before re-running."
)


def _failure_lines(payload: dict[str, Any]) -> list[str]:
    status = payload.get("status") or ""
    lines = [f"Error: {payload.get('error') or 'not recorded'}"]
    if status == "timed_out_unknown":
        lines.append(_OUTCOME_UNKNOWN_LINE)
    return lines


def _fmt_failure_detail(
    *,
    error_type: str | None,
    traceback: str | None,
    step_id: str | None,
    step_kind: str | None,
    step_error: str | None,
    step_inputs: Any,
) -> list[str]:
    """plans/035 P1.5: the failure detail an agent turn needs to ACT on a
    failed run — the exception type, the traceback (capped), the failing step
    and its inputs — instead of only "Error: <one line>" plus a pointer to
    playbook_status that the turn rarely followed."""
    lines: list[str] = []
    if error_type:
        lines.append(f"Error type: {error_type}")
    if step_id:
        head = f"Failing step: {step_id}"
        if step_kind:
            head += f" ({step_kind})"
        lines.append(head)
        if step_error and step_error.strip():
            lines.append(f"Step error: {step_error.strip()[:_STEP_INPUTS_CAP]}")
        if step_inputs:
            text = json.dumps(step_inputs, indent=None, default=str)
            if len(text) > _STEP_INPUTS_CAP:
                text = text[:_STEP_INPUTS_CAP] + "... (truncated)"
            lines.append(f"Step inputs: {text}")
    if traceback and traceback.strip():
        tb = traceback.strip()
        if len(tb) > _TRACEBACK_CAP:
            tb = tb[-_TRACEBACK_CAP:]
            tb = "... (earlier frames cut)\n" + tb
        lines.append(f"Traceback:\n{tb}")
    return lines


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
        failed = (payload.get("status") or "") in FAILED_RUN_STATUSES
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

    async def _failure_detail(self, run_id: Any) -> list[str]:
        """Load the run's error contract and the failing step (plans/035 P1.5)."""
        rid = run_id
        if isinstance(rid, str):
            try:
                rid = uuid.UUID(rid)
            except ValueError:
                return []
        try:
            async with self._sf() as session:
                run = await session.get(PlaybookRun, rid)
                steps = (await session.execute(
                    select(PlaybookStepRun).where(PlaybookStepRun.run_id == rid)
                )).scalars().all()
        except Exception:  # noqa: BLE001 — detail is best-effort, the moment still goes
            log.exception("run_wake.failure_detail_failed run=%s", run_id)
            return []
        failing = None
        for s in steps:
            if s.status == "failed" or (s.error and s.status != "done"):
                if failing is None or (s.started_at and failing.started_at
                                       and _aware(s.started_at) > _aware(failing.started_at)):
                    failing = s
        return _fmt_failure_detail(
            error_type=getattr(run, "error_type", None) if run else None,
            traceback=getattr(run, "traceback", None) if run else None,
            step_id=failing.step_id if failing else None,
            step_kind=failing.step_kind if failing else None,
            step_error=failing.error if failing else None,
            step_inputs=failing.inputs if failing else None,
        )

    @staticmethod
    def _log_send_result(kind: str, run_id: Any, result: Any) -> None:
        """plans/035 P1.5: the moment's turn result was discarded, so a wake
        turn cut at its budget (`aborted`) or dead (`error`) logged as
        delivered. Name the real outcome."""
        if isinstance(result, dict):
            if result.get("aborted"):
                log.warning(
                    "run_wake.moment_aborted kind=%s run=%s reason=%s",
                    kind, run_id, result.get("aborted"),
                )
                return
            if result.get("error"):
                log.warning(
                    "run_wake.moment_failed kind=%s run=%s error=%s",
                    kind, run_id, str(result.get("error"))[:200],
                )
                return
        log.info("run_wake.%s run=%s", kind, run_id)

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
        if status in FAILED_RUN_STATUSES:
            lines.extend(_failure_lines(payload))
            lines.extend(await self._failure_detail(run_id))
            lines.append("")
            lines.append(
                "Report the failure to the owner honestly — do NOT fabricate "
                "results. playbook_status(run_id) shows the full trace. "
                + _CONTINUE_AFTER_FAILURE
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
            result = await send(
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
            self._log_send_result("watch_moment", run_id, result)
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
        if status in FAILED_RUN_STATUSES:
            lines.extend(_failure_lines(payload))
            lines.extend(await self._failure_detail(run_id))
            lines.append("")
            lines.append(
                "Report the failure to the owner honestly — do NOT fabricate "
                "results. playbook_status(run_id) shows the full trace. "
                + _CONTINUE_AFTER_FAILURE
            )
        else:
            # plans/032 phase 08: a python run's return value leads (the
            # payload's additive `result` key), the step outputs follow
            if payload.get("result") is not None:
                text = json.dumps(payload["result"], indent=2, default=str)
                if len(text) > _OUTPUTS_CAP:
                    text = text[:_OUTPUTS_CAP] + "\n... (truncated)"
                lines.append("")
                lines.append(f"Result:\n{text}")
            outputs = await self._collect_outputs(run_id)
            if outputs:
                lines.append("")
                lines.append(f"Step outputs:\n{outputs}")
            elif payload.get("result") is None:
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
            result = await send(
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
            self._log_send_result("moment", run_id, result)
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
        if status in FAILED_RUN_STATUSES:
            body += " " + " ".join(_failure_lines(payload))
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
