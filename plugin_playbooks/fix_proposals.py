"""0.26.0 (plans/015, 089 §4): fix proposals for production failures.

A failed LIVE run (never a test run) produces ONE open fix proposal per
(playbook, failure signature) — a ledger row that dedupes repeats and
counts them. plans/016 phase 2: each new or bumped proposal sends ONE
muted wake message directly to the ops chat (no bus contract, no approval
card): what failed, the error, the count. The wake turn investigates and
fixes right away; the owner's single approval happens at publish, where
the card shows the change and the gate-status bullets (021).

On cores without an ops chat the service still records proposals in
`playbook_fix_proposals`, so nothing is lost — the failure digest keeps
surfacing them in the agent's prompt sections.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    FAILED_RUN_STATUSES, Playbook, PlaybookFixProposal, PlaybookRun, PlaybookStepRun,
)
from .publish import ops_conversation_id
from .versioning import live_version_of

log = logging.getLogger("luna.playbooks.fix_proposals")


_V2_LINE_RE = re.compile(r"^line (\d+):")
_V2_TAIL_RE = re.compile(r" (after effect \S+|before any effect)\s*$")


def _v2_signature_parts(one_liner: str) -> tuple[str, str]:
    """`line 7: rows['items'] → KeyError: 'items' after effect fetch#1` →
    ("line 7", "KeyError: 'items'"). Falls back to ("", whole text)."""
    m = _V2_LINE_RE.match(one_liner or "")
    step = f"line {m.group(1)}" if m else ""
    head = one_liner or ""
    if " → " in head:
        head = head.split(" → ", 1)[1]
    head = _V2_TAIL_RE.sub("", head).strip()
    return step, head


def failure_signature(playbook_name: str, step_id: str, error: str) -> str:
    """Stable identity of a failure across repeats: playbook + failed step +
    the head of the error with volatile digits stripped."""
    head = "".join(c for c in (error or "")[:160] if not c.isdigit()).strip()
    raw = f"{playbook_name}|{step_id}|{head}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


class FixProposalService:
    """Subscribes to `playbook.run.completed` and files fix proposals."""

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
        # keep spawned proposal tasks referenced (asyncio drops weak refs)
        self._tasks: set[asyncio.Task] = set()

    def start(self) -> None:
        # background=True: the real work runs in its own task so a blocking
        # send can never stall the emitting run's completion path.
        self._unsub = self._events.subscribe(
            "playbook.run.completed", self._on_completed, background=True,
        )
        log.info("fix_proposals.started")

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
        # plans/032 phase 08: `timed_out_unknown` files a proposal like a
        # failure (error_type OutcomeUnknown rides on the card); `parked`
        # never reaches here (no completion event) and is never a failure.
        if payload.get("status") not in FAILED_RUN_STATUSES or payload.get("is_test"):
            return
        task = asyncio.create_task(
            self._file_proposal(payload), name="playbook-fix-proposal",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _file_proposal(self, payload: dict[str, Any]) -> None:
        try:
            await self._file_proposal_inner(payload)
        except Exception:  # noqa: BLE001 — never let a proposal error escape
            log.exception("fix_proposal.failed payload=%s", payload)

    async def _file_proposal_inner(self, payload: dict[str, Any]) -> None:
        run_id = payload.get("run_id")
        if isinstance(run_id, str):
            try:
                import uuid as _uuid
                run_id = _uuid.UUID(run_id)
            except ValueError:
                return
        async with self._sf() as session:
            run = await session.get(PlaybookRun, run_id)
            if run is None or run.is_test:
                return
            playbook = await session.get(Playbook, run.playbook_id)
            if playbook is None or playbook.status != "enabled":
                return
            # proposals are for PRODUCTION failures: the version that failed
            # must still be the live one (a failure of an since-replaced
            # version is stale news, and the digest covers history anyway).
            # plans/032 phase 04: None = candidate-only row; nothing is
            # live, so a failure there is never a production failure.
            live = live_version_of(playbook)
            if live is None or run.playbook_version != live:
                return
            failed_step = (await session.execute(
                select(PlaybookStepRun).where(
                    PlaybookStepRun.run_id == run.id,
                    PlaybookStepRun.status == "failed",
                ).order_by(PlaybookStepRun.started_at).limit(1)
            )).scalar_one_or_none()
            step_id = failed_step.step_id if failed_step else ""
            error = (
                (failed_step.error if failed_step else None)
                or run.error or payload.get("error") or ""
            )
            sig_step, sig_error = step_id, error
            if getattr(run, "error_type", None) and not failed_step:
                # plans/032 phase 04 (docs/v2.md §7): a python failure keys
                # its signature on the line and the `<type>: <message>`
                # head of the one-liner — same line, same error → one row.
                sig_step, sig_error = _v2_signature_parts(run.error or "")
                step_id = sig_step or step_id
            sig = failure_signature(playbook.name, sig_step, sig_error)
            existing = (await session.execute(
                select(PlaybookFixProposal).where(
                    PlaybookFixProposal.playbook_id == playbook.id,
                    PlaybookFixProposal.signature == sig,
                    PlaybookFixProposal.status == "open",
                )
            )).scalars().first()
            # plans/018 phase 2 / plans/019: "what the playbook does" =
            # first manifest CONTENT line, else its description. A manifest
            # usually opens with a bare "# Purpose" heading — that's a label,
            # not the purpose; skip headings and take the first prose line.
            purpose = (playbook.description or "").strip()
            for line in (playbook.manifest or "").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    purpose = line
                    break
            # plans/016 phase 2: everything the wake message needs, read
            # while the rows are in hand.
            card = {
                "display": playbook.display_name or playbook.name,
                "purpose": purpose,
                "step_id": step_id,
                "run_id": str(run.id),
                "version": run.playbook_version,
                "error": (error or "")[:400],
                # plans/032 phase 08: `OutcomeUnknown` on a timed_out_unknown
                # run — the wake says so; the fixer checks the target first
                "error_type": getattr(run, "error_type", None),
                "status": run.status,
            }
            playbook_name = playbook.name
            if existing is not None:
                # dedupe: one open proposal per (playbook, signature) — a
                # repeat bumps the count, no second ledger row.
                existing.failure_count += 1
                existing.last_run_id = run.id
                existing.updated_at = datetime.now(timezone.utc)
                count = existing.failure_count
                await session.commit()
                log.info(
                    "fix_proposal.repeat playbook=%s count=%d",
                    playbook_name, count,
                )
            else:
                title = (
                    f"Fix playbook '{playbook_name}': {step_id or 'run'} failing"
                )
                diagnosis = (
                    f"Live run {run.id} of version {run.playbook_version} failed"
                    + (f" at step '{step_id}'" if step_id else "")
                    + (f": {error[:400]}" if error else ".")
                )
                proposal = PlaybookFixProposal(
                    playbook_id=playbook.id,
                    signature=sig,
                    title=title,
                    diagnosis=diagnosis,
                    last_run_id=run.id,
                )
                session.add(proposal)
                await session.commit()
                await session.refresh(proposal)
                count = 1
                log.info(
                    "fix_proposal.filed playbook=%s id=%s",
                    playbook_name, proposal.id,
                )
        await self._wake(playbook_name, card, count)

    async def _wake(
        self, playbook_name: str, card: dict[str, Any], count: int,
    ) -> None:
        """plans/016 phase 2: one muted message to the ops chat per new or
        bumped proposal — the failure wake. Direct send, no bus contract.

        The wake turn is expected to investigate and fix immediately; the
        owner's consent lives at the publish approval card, so the old
        approve-a-fix-attempt card is gone. Since luna
        098 the ops chat always runs in the ordinary 'building' state, so
        the wake turn has the full toolset without any state juggling.
        """
        ctx = self._ctx
        if ctx is None:
            return
        ops = await ops_conversation_id(ctx)
        send = getattr(ctx, "send_muted_message", None)
        if ops is None or send is None:
            # headless test ctx, or a broken ops lookup: ledger row only —
            # surfaced via the failure digest.
            return
        display = card.get("display") or playbook_name
        purpose = card.get("purpose") or ""
        step_id = card.get("step_id") or ""
        times = "once" if count == 1 else f"{count} times"
        body_lines = [
            f"The '{display}' playbook"
            + (f" ({purpose})" if purpose else "")
            + " failed on a real run"
            + (f" at the '{step_id}' step" if step_id else "")
            + f". It has now failed {times}.",
            f"Run: {card.get('run_id', '?')} (version {card.get('version', '?')})",
            f"Error: {card.get('error') or 'not recorded'}",
            *(
                [
                    "Outcome unknown (OutcomeUnknown): an effect was in flight "
                    "when the process died and its result was never recorded — "
                    "check the target system before assuming it did or did "
                    "not happen.",
                ]
                if card.get("status") == "timed_out_unknown"
                or card.get("error_type") == "OutcomeUnknown"
                else []
            ),
            "",
            "Investigate now: playbook_status(run_id) shows the failing "
            "trace. If a change is needed, fix the candidate, test it for "
            "real (playbook_run_candidate), and publish — the publish "
            "approval card is where the owner decides. If nothing should "
            "change, say so briefly here instead.",
        ]
        try:
            await send(
                f"Playbook failing: {playbook_name}",
                "\n".join(body_lines),
                channel="moment",
                respond=True,
                conversation_id=ops,
                source="playbooks",
                # without tools the wake turn cannot diagnose anything.
                # "all" + the core's state gating scopes it to the ops
                # chat's mode.
                tools="all",
            )
            log.info(
                "fix_proposal.wake_sent playbook=%s count=%d",
                playbook_name, count,
            )
        except Exception:  # noqa: BLE001
            log.exception("fix_proposal.wake_failed playbook=%s", playbook_name)
