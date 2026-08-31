"""0.26.0 (plans/015, 089 §4): fix proposals for production failures.

A failed LIVE run (never a test run) produces ONE open fix proposal per
(playbook, failure signature) — an approval-shaped card in the ops chat via
the existing approvals machinery (089 contract #7). Repeated identical
failures update the open proposal's count instead of filing another card.
Approving the card wakes the ops chat to do the fix through its normal,
state-gated tools; publishing still goes through the playbook_publish gate.

On cores without an ops chat or approvals surface the service still records
proposals in `playbook_fix_proposals` (the ledger doubles as the dedupe
key), so nothing is lost — the cards appear once the core provides a home
for them.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Playbook, PlaybookFixProposal, PlaybookRun, PlaybookStepRun
from .publish import ops_conversation_id

log = logging.getLogger("luna.playbooks.fix_proposals")


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
        # approval card can never stall the emitting run's completion path.
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
        if payload.get("status") != "failed" or payload.get("is_test"):
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
            live = playbook.live_version or playbook.version
            if run.playbook_version != live:
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
                or payload.get("error") or ""
            )
            sig = failure_signature(playbook.name, step_id, error)
            existing = (await session.execute(
                select(PlaybookFixProposal).where(
                    PlaybookFixProposal.playbook_id == playbook.id,
                    PlaybookFixProposal.signature == sig,
                    PlaybookFixProposal.status == "open",
                )
            )).scalars().first()
            if existing is not None:
                # dedupe: one open proposal per (playbook, signature) — a
                # repeat bumps the count, no second card.
                existing.failure_count += 1
                existing.last_run_id = run.id
                existing.updated_at = datetime.now(timezone.utc)
                await session.commit()
                log.info(
                    "fix_proposal.repeat playbook=%s count=%d",
                    playbook.name, existing.failure_count,
                )
                return
            title = f"Fix playbook '{playbook.name}': {step_id or 'run'} failing"
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
            proposal_id = proposal.id
            playbook_name = playbook.name
            # plans/018 phase 2: everything the owner-facing card needs, read
            # while the rows are in hand. "What the playbook does" = first
            # manifest line, else its description.
            purpose = (
                (playbook.manifest or "").strip().splitlines()[:1]
                or [(playbook.description or "").strip()]
            )[0].lstrip("# ").strip()
            card = {
                "display": playbook.display_name or playbook.name,
                "purpose": purpose,
                "step_id": step_id,
                "run_id": str(run.id),
                "version": run.playbook_version,
                "error": (error or "")[:400],
            }
        log.info("fix_proposal.filed playbook=%s id=%s", playbook_name, proposal_id)
        await self._post_card(proposal_id, playbook_name, title, diagnosis, card)

    async def _post_card(
        self,
        proposal_id: Any,
        playbook_name: str,
        title: str,
        diagnosis: str,
        card: dict[str, Any] | None = None,
    ) -> None:
        """Post the proposal as an approval card in the ops chat. Approval
        wakes the ops chat to do the fix; denial dismisses the proposal."""
        ctx = self._ctx
        ops = await ops_conversation_id(ctx)
        # plans/018 phase 2: `ctx.approval` RAISES when the engine is unwired
        # (it is a property, so getattr's default never applies) — and the
        # outer catch-all used to eat the card silently.
        approval = None
        if ctx is not None:
            try:
                approval = ctx.approval
            except Exception:  # noqa: BLE001 — unwired engine: ledger-only path
                approval = None
        if ops is None or approval is None:
            # headless test ctx, or a broken ops lookup: ledger row only —
            # surfaced via the failure digest.
            return
        card = card or {}
        display = card.get("display") or playbook_name
        purpose = card.get("purpose") or ""
        step_id = card.get("step_id") or ""
        explanation_bits = [
            f"The **{display}** playbook"
            + (f" ({purpose})" if purpose else "")
            + " failed on a real run"
            + (f" at the '{step_id}' step" if step_id else "")
            + ".",
            "Approving lets me work on a fix right away. Any fix is tested "
            "first, and nothing goes live without a second approval from "
            "you. Denying dismisses this proposal.",
        ]
        # dedup keeps the FIRST pending card's presentation — repeats bump
        # the ledger count, so the card intentionally names no count.
        presentation = {
            "eyebrow": "Playbook failing",
            "headline": f"'{display}' is failing — approve a fix attempt",
            "explanation": "\n\n".join(explanation_bits),
            "changes": [{
                "label": "Failure detail",
                "kind": "text",
                "text": (
                    f"Run: {card.get('run_id', '?')}\n"
                    f"Version: {card.get('version', '?')}\n"
                    + (f"Step: {step_id}\n" if step_id else "")
                    + f"Error: {card.get('error') or 'not recorded'}"
                ),
            }],
        }
        try:
            result = await approval.request(
                kind="playbook_fix_proposal",
                summary=f"{title} — approve to have a fix worked on now.",
                payload={
                    "proposal_id": str(proposal_id),
                    "playbook": playbook_name,
                    "target_ref": f"playbook:{playbook_name}",
                },
                requested_by_plugin="plugin-playbooks",
                risk_level="medium",
                conversation_id=ops,
                presentation=presentation,
            )
        except Exception:  # noqa: BLE001
            log.exception("fix_proposal.card_failed playbook=%s", playbook_name)
            return
        # 0.30.1: the real ApprovalDecision carries decision="approved" — it
        # has no `approved` attribute, so the old attr-only read dismissed
        # every approved card. Accept both shapes.
        decision = (
            getattr(result, "decision", None)
            or (isinstance(result, dict) and result.get("decision"))
        )
        approved = bool(
            decision == "approved"
            or getattr(result, "approved", None)
            or (isinstance(result, dict) and result.get("approved"))
        )
        async with self._sf() as session:
            proposal = await session.get(PlaybookFixProposal, proposal_id)
            if proposal is not None and proposal.status == "open":
                proposal.status = "approved" if approved else "dismissed"
                await session.commit()
        if approved:
            # 0.30.2 (luna 095): the wake turn inherits the ops chat's STORED
            # state — in `identify` every mutation tool is absent by mode
            # gating (tools="all" cannot override it), so the agent woke
            # unable to fix anything. The owner's approval IS the consent to
            # leave diagnose-only mode: advance identify → fix_publish, where
            # the machine-checked publish gate + the playbook's publish
            # autonomy still hold the promised second approval. only_from
            # keeps us from downgrading a chat the owner already moved.
            # Older cores (< 0.91.001) lack the API — degrade visible: the
            # wake still fires and the agent reports what mode it needs.
            set_state = getattr(ctx, "set_conversation_state", None)
            if set_state is not None:
                try:
                    await set_state(ops, "fix_publish", only_from="identify")
                except Exception:  # noqa: BLE001
                    log.exception(
                        "fix_proposal.state_flip_failed playbook=%s",
                        playbook_name,
                    )
            send = getattr(ctx, "send_muted_message", None)
            if send is None:
                return
            try:
                await send(
                    f"Approved fix: {playbook_name}",
                    f"The owner approved fixing playbook '{playbook_name}'.\n"
                    f"{diagnosis}\n"
                    "Diagnose and fix it now with the playbook tools "
                    "available in this chat's current mode. Test the fix "
                    "(playbook_run_candidate); publishing goes through "
                    "playbook_publish's test gate.",
                    channel="moment",
                    respond=True,
                    conversation_id=ops,
                    source="playbooks",
                    # plans/018 phase 2: without tools the wake turn cannot
                    # diagnose anything. "all" + the core's state gating
                    # (kind/state ride into moment turns since luna 0.91)
                    # scopes it to the ops chat's mode.
                    tools="all",
                )
            except Exception:  # noqa: BLE001
                log.exception("fix_proposal.wake_failed playbook=%s", playbook_name)
