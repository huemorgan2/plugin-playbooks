"""plans/016 phase 5 — THE way a version number is minted.

`playbooks.version` is the monotonic counter; a `playbook_versions` row
holds the content OF a number; specs (tests) belong to a number too. Every
new number is minted here so the row and its inherited spec set are never
out of step. Four call sites: owner PUT definition, owner/agent manifest
save, agent candidate save.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Playbook, PlaybookSpec, PlaybookVersion


def live_version_of(p: Playbook) -> int:
    return p.live_version or p.version


async def get_version_row(
    session: AsyncSession, p: Playbook, n: int,
) -> PlaybookVersion | None:
    # Legacy DBs can hold DUPLICATE rows for one number (the pre-0.32 edit
    # path snapshotted at the counter even when a row existed) — pick
    # deterministically instead of scalar_one_or_none() 500ing: real lineage
    # (promoted_from set) beats a plain snapshot, then oldest wins.
    rows = (await session.execute(
        select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == p.id,
            PlaybookVersion.version == n,
        )
    )).scalars().all()
    if not rows:
        return None
    return min(rows, key=lambda r: (r.promoted_from is None, r.created_at))


async def ensure_live_row(session: AsyncSession, p: Playbook) -> PlaybookVersion:
    """Guarantee a version row exists for the current live content. Records
    an EXISTING number — no new number, no spec copy (the live number's
    specs are already its own after the load-time backfill)."""
    n = live_version_of(p)
    row = await get_version_row(session, p, n)
    if row is None:
        row = PlaybookVersion(
            playbook_id=p.id,
            version=n,
            definition=p.definition,
            code=p.code,
            manifest=p.manifest,
            author="system",
            message="live content (recorded on first candidate/promote)",
        )
        session.add(row)
    return row


async def copy_specs(
    session: AsyncSession, playbook_id: Any, from_version: int, to_version: int,
) -> int:
    """Duplicate every spec of `from_version` onto `to_version` with a fresh
    result cache (they have not run against the new content). Names already
    present on the target are left alone. Returns the number copied."""
    if from_version == to_version:
        return 0
    src = (await session.execute(
        select(PlaybookSpec).where(
            PlaybookSpec.playbook_id == playbook_id,
            PlaybookSpec.playbook_version == from_version,
        )
    )).scalars().all()
    if not src:
        return 0
    existing = set((await session.execute(
        select(PlaybookSpec.name).where(
            PlaybookSpec.playbook_id == playbook_id,
            PlaybookSpec.playbook_version == to_version,
        )
    )).scalars().all())
    n = 0
    for s in src:
        if s.name in existing:
            continue
        session.add(PlaybookSpec(
            playbook_id=playbook_id,
            playbook_version=to_version,
            name=s.name,
            spec=s.spec,
            created_by=s.created_by,
            last_result=None,
            last_run_at=None,
            last_version=None,
        ))
        n += 1
    return n


async def mint_version(
    session: AsyncSession,
    p: Playbook,
    *,
    definition: dict,
    code: str | None,
    manifest: str,
    author: str,
    message: str,
    source_version: int,
    promoted_from: int | None = None,
) -> PlaybookVersion:
    """Increment `p.version`, add the row for the new number and inherit
    `source_version`'s specs. Callers decide what the number means (move
    `live_version` / `candidate_version` themselves) and commit."""
    from sqlalchemy import func

    # Mint ABOVE any stored row, not just above the counter — a counter that
    # fell behind the rows (legacy damage) must never re-issue a taken number.
    max_stored = (await session.execute(
        select(func.max(PlaybookVersion.version)).where(
            PlaybookVersion.playbook_id == p.id,
        )
    )).scalar() or 0
    p.version = max(p.version, max_stored) + 1
    row = PlaybookVersion(
        playbook_id=p.id,
        version=p.version,
        definition=definition,
        code=code,
        manifest=manifest,
        author=author,
        message=message,
        promoted_from=promoted_from,
    )
    session.add(row)
    await copy_specs(session, p.id, source_version, p.version)
    return row


async def heal_duplicate_version_rows(session_factory) -> int:
    """0.38.0: delete redundant duplicate (playbook_id, version) rows.

    The pre-0.32 whole-YAML edit path snapshotted at the current counter even
    when that number already had a row, leaving e.g. two v33s — the Versions
    list showed both and version reads 500ed (MultipleResultsFound). Keeps
    the same row `get_version_row` prefers (real lineage first, then oldest)
    and drops the rest. Idempotent; returns rows deleted."""
    import logging

    log = logging.getLogger("luna.plugin.playbooks")
    deleted = 0
    async with session_factory() as session:
        rows = (await session.execute(
            select(PlaybookVersion).order_by(
                PlaybookVersion.playbook_id, PlaybookVersion.version,
            )
        )).scalars().all()
        by_number: dict[tuple, list[PlaybookVersion]] = {}
        for r in rows:
            by_number.setdefault((r.playbook_id, r.version), []).append(r)
        for (pid, n), group in by_number.items():
            if len(group) < 2:
                continue
            keep = min(group, key=lambda r: (r.promoted_from is None, r.created_at))
            for r in group:
                if r.id != keep.id:
                    await session.delete(r)
                    deleted += 1
            log.info(
                "playbooks: healed %d duplicate row(s) of version %d "
                "(playbook %s)", len(group) - 1, n, pid,
            )
        if deleted:
            await session.commit()
    return deleted


def spec_source_version(p: Playbook) -> int:
    """The version an edit starts from: the candidate when one exists (the
    agent iterates on it, its tests are the newest), else live."""
    return p.candidate_version or live_version_of(p)
