"""Test helper: satisfy playbook_publish's test-run gate (0.26.0, 089 #8).

Inserts a completed green PlaybookRun of exactly `version`, started after the
version row was created, so the gate sees fresh evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from plugin_playbooks.models import Playbook, PlaybookRun


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
