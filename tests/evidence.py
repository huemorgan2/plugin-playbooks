"""Test helper: satisfy playbook_publish's test-run gate (0.26.0, 089 #8).

Inserts a completed green PlaybookRun of exactly `version`, started after the
version row was created, so the gate sees fresh evidence.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from plugin_playbooks.models import Playbook, PlaybookRun

# plans/018 phase 1: publish/rollback require an owner-language explanation
# (>= 80 chars, handler-enforced). Shared valid value for every test that is
# not itself about the explanation gate.
EXPLANATION = (
    "The greeter playbook needed a small change to keep working. This "
    "version fixes the reported problem and was tested with a green run "
    "before publishing. Nothing else about the playbook changes."
)

# plans/016 phase 1: every publish/rollback runs under a plan. Shared valid
# body (>= 100 chars, owner language) for every test that is not itself
# about the plan gate.
PLAN_BODY = (
    "The greeter playbook greets people by the wrong field. I will change "
    "it to use the person's name, test the candidate with a real run, and "
    "publish once the run is green. Nothing else changes."
)


async def make_plan(tools, *, title: str = "Fix the greeter playbook") -> str:
    """Write a valid plan through the real tool; returns its plan_id."""
    out = json.loads(await tools["playbook_plan_write"](
        title=title, body=PLAN_BODY, playbook_refs=["greeter"],
    ))
    assert "plan_id" in out, out
    return out["plan_id"]


async def seed_plan(sf, *, title: str = "Fix the greeter playbook") -> str:
    """Insert a plan row directly (for route tests without the tool dict)."""
    from plugin_playbooks.models import PlaybookPlan
    async with sf() as s:
        p = PlaybookPlan(title=title, body=PLAN_BODY, playbook_refs=["greeter"])
        s.add(p)
        await s.commit()
        await s.refresh(p)
        return str(p.id)


async def green_run(
    sf, version: int, *, name: str | None = None, is_test: bool = True,
) -> None:
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    async with sf() as s:
        q = select(Playbook)
        if name is not None:
            q = q.where(Playbook.name == name)
        pb = (await s.execute(q)).scalars().first()
        s.add(PlaybookRun(
            playbook_id=pb.id, playbook_version=version, status="done",
            trigger="agent-candidate" if is_test else "schedule",
            is_test=is_test, started_at=later,
            completed_at=later + timedelta(seconds=1),
        ))
        await s.commit()
