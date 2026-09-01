"""0.32.0 (luna-plugins plans/016): playbook change plans.

The whole plans feature is one table and one rule: every publish (or
rollback) must name a plan the owner can read. Everything here is a thin
helper over `PlaybookPlan`; the enforcement itself lives in the publish
path (`agent_tools._do_publish`, `routes.promote_version`) — the ONLY
tool-layer gate in the design.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PlaybookAppSetting, PlaybookPlan

log = logging.getLogger("luna.plugin.playbooks.plans")

FULL_POWER_KEY = "plans_full_power"

# statuses a publish may run under: a fresh plan, or one that already carried
# an earlier publish (one plan can cover several playbooks/publishes).
PUBLISHABLE_STATUSES = ("proposed", "approved")

_PLAN_HINT = (
    "Every playbook change requires a plan the owner can read. Write one "
    "with playbook_plan_write(title, body, playbook_refs) — plain words: "
    "what is wrong, what you will change, why it is safe — then call this "
    "tool again with its plan_id."
)


def plan_brief(plan: PlaybookPlan) -> dict[str, Any]:
    return {
        "plan_id": str(plan.id),
        "title": plan.title,
        "status": plan.status,
        "playbook_refs": list(plan.playbook_refs or []),
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        "has_execution_summary": bool(plan.execution_summary),
    }


def plan_detail(plan: PlaybookPlan) -> dict[str, Any]:
    d = plan_brief(plan)
    d.update({
        "body": plan.body,
        "rejection_note": plan.rejection_note,
        "execution_summary": plan.execution_summary,
        "outcome_facts": plan.outcome_facts,
        "conversation_id": (
            str(plan.conversation_id) if plan.conversation_id else None
        ),
    })
    return d


def _parse_plan_id(plan_id: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(plan_id))
    except (ValueError, TypeError, AttributeError):
        return None


async def load_plan(
    session: AsyncSession, plan_id: Any,
) -> PlaybookPlan | None:
    pid = _parse_plan_id(plan_id)
    if pid is None:
        return None
    return (await session.execute(
        select(PlaybookPlan).where(PlaybookPlan.id == pid)
    )).scalar_one_or_none()


async def plan_gate(
    session: AsyncSession, plan_id: Any,
) -> tuple[PlaybookPlan | None, str | None]:
    """The publish gate's plan check: (plan, None) when a publish may proceed
    under `plan_id`, (maybe-plan, refusal-json) otherwise."""
    if not plan_id:
        return None, json.dumps({
            "error": "Refused — no `plan_id` given. Nothing was changed.",
            "gate": "plan_required",
            "hint": _PLAN_HINT,
        })
    plan = await load_plan(session, plan_id)
    if plan is None:
        return None, json.dumps({
            "error": f"Refused — no plan with id '{plan_id}'. "
                     "Nothing was changed.",
            "gate": "plan_required",
            "hint": _PLAN_HINT + " playbook_plan_read() lists existing plans.",
        })
    if plan.status not in PUBLISHABLE_STATUSES:
        return plan, json.dumps({
            "error": (
                f"Refused — plan '{plan.title}' is {plan.status}, and a "
                f"{plan.status} plan cannot carry a publish. "
                "Nothing was changed."
            ),
            "gate": "plan_required",
            "plan_id": str(plan.id),
            "rejection_note": plan.rejection_note,
            "hint": (
                "Write a new plan with playbook_plan_write"
                + (
                    " — address the owner's rejection note."
                    if plan.status == "rejected" else "."
                )
            ),
        })
    return plan, None


async def stamp_outcome(
    session: AsyncSession, plan_id: Any, facts: dict[str, Any],
) -> None:
    """Append a code-stamped publish record to the plan and mark it approved.
    Called by the publish path AFTER the flip committed; never raises — a
    stamping failure must not undo a publish."""
    try:
        plan = await load_plan(session, plan_id)
        if plan is None:
            return
        record = dict(facts)
        record["at"] = datetime.now(timezone.utc).isoformat()
        plan.outcome_facts = list(plan.outcome_facts or []) + [record]
        if plan.status == "proposed":
            plan.status = "approved"
        await session.commit()
    except Exception:  # noqa: BLE001
        log.exception("outcome stamp failed plan=%s", plan_id)


async def record_rejection(
    session: AsyncSession, plan_id: Any, reason: str | None,
) -> None:
    """Owner rejected the publish card raised under this plan. Best-effort."""
    try:
        plan = await load_plan(session, plan_id)
        if plan is None or plan.status not in PUBLISHABLE_STATUSES:
            return
        plan.status = "rejected"
        plan.rejection_note = (reason or "").strip() or None
        await session.commit()
    except Exception:  # noqa: BLE001
        log.exception("rejection record failed plan=%s", plan_id)


async def full_power(session: AsyncSession) -> bool:
    row = (await session.execute(
        select(PlaybookAppSetting).where(
            PlaybookAppSetting.key == FULL_POWER_KEY
        )
    )).scalar_one_or_none()
    return bool(row is not None and (row.value or {}).get("on"))


async def set_full_power(session: AsyncSession, on: bool) -> None:
    row = (await session.execute(
        select(PlaybookAppSetting).where(
            PlaybookAppSetting.key == FULL_POWER_KEY
        )
    )).scalar_one_or_none()
    if row is None:
        session.add(PlaybookAppSetting(key=FULL_POWER_KEY, value={"on": on}))
    else:
        row.value = {"on": on}
    await session.commit()
