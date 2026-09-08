"""plans/016 phase 5 — THE way a version number is minted.

`playbooks.version` is the monotonic counter; a `playbook_versions` row
holds the content OF a number. Every new number is minted here so the
counter and the row are never out of step. Four call sites: owner PUT
definition, owner/agent manifest save, agent candidate save.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Playbook, PlaybookVersion


def live_version_of(p: Playbook) -> int | None:
    """The version triggers and `playbook_run` execute, or None when nothing
    is live yet. `live_version == 0` reads "same as version" on legacy rows
    (no candidate pointer — both columns arrived together in 0.10.0) and
    "no live version" on a row that carries a candidate (plans/032 phase 04:
    propose saves a candidate; the first publish makes it live)."""
    if p.live_version == 0 and p.candidate_version is not None:
        return None
    return p.live_version or p.version


def _dup_keep_key(r: PlaybookVersion) -> tuple:
    """plans/022 P6: ONE ordering for picking among duplicate version rows,
    shared by get_version_row and the healer (they must never disagree on
    which row survives). Content wins before lineage before age — during the
    meltdown the healer kept "the oldest" and only luck made that the row
    with content."""
    return (
        not (r.definition and (r.definition.get("steps") or ())),  # has steps
        not r.code,          # then non-empty code
        not r.manifest,      # then non-empty manifest
        r.promoted_from is None,  # then real lineage
        r.created_at,        # then oldest
    )


async def get_version_row(
    session: AsyncSession, p: Playbook, n: int,
) -> PlaybookVersion | None:
    # Legacy DBs can hold DUPLICATE rows for one number (the pre-0.32 edit
    # path snapshotted at the counter even when a row existed) — pick
    # deterministically instead of scalar_one_or_none() 500ing, preferring
    # rows with real content (_dup_keep_key).
    rows = (await session.execute(
        select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == p.id,
            PlaybookVersion.version == n,
        )
    )).scalars().all()
    if not rows:
        return None
    return min(rows, key=_dup_keep_key)


def shim_playbook(playbook: Playbook, row: PlaybookVersion) -> Playbook:
    """Transient Playbook carrying a version row's content — NEVER added to a
    session. plans/032 phase 06 (Risks 11): the shape `agent_tools` builds
    for candidate runs, hoisted here so `runner._resume_run` can execute a
    run on the exact version row it started on (the runner only reads
    id/name/display_name/definition/code/format/live_version)."""
    return Playbook(
        id=playbook.id,
        name=playbook.name,
        display_name=playbook.display_name,
        description=playbook.description,
        when_to_use=playbook.when_to_use,
        inputs_schema=dict(row.definition or {}).get("inputs"),
        definition=row.definition,
        code=row.code,
        # phase 08: the version row's own language (a candidate may differ
        # from the live format).
        format=row.format or playbook.format,
        manifest=row.manifest,
        version=row.version,
        live_version=row.version,
        status=playbook.status,
        agent_autonomy=playbook.agent_autonomy,
    )


async def ensure_live_row(session: AsyncSession, p: Playbook) -> PlaybookVersion | None:
    """Guarantee a version row exists for the current live content. Records
    an EXISTING number — no new number is minted. Returns None (and creates
    nothing) when the playbook has no live version yet."""
    n = live_version_of(p)
    if n is None:
        return None
    row = await get_version_row(session, p, n)
    if row is None:
        row = PlaybookVersion(
            playbook_id=p.id,
            version=n,
            definition=p.definition,
            code=p.code,
            manifest=p.manifest,
            format=p.format or "pblang",
            author="system",
            message="live content (recorded on first candidate/promote)",
        )
        session.add(row)
    return row


def author_label(author: str | None) -> str:
    """plans/032 phase 11: the owner-facing name of a version row's author —
    `agent` → "the agent", `owner` → "the owner", `delegation:<id>` →
    "delegation <8 hex> (delegation:<id>)"; anything else verbatim."""
    a = author or ""
    if a == "agent":
        return "the agent"
    if a == "owner":
        return "the owner"
    if a.startswith("delegation:"):
        return f"delegation {a[len('delegation:'):][:8]} ({a})"
    return a or "an unknown author"


async def candidate_conflict(
    session: AsyncSession, p: Playbook, author: str,
) -> dict | None:
    """plans/032 phase 11: the candidate-conflict guard. None when the
    playbook has no unpublished candidate or its candidate row was written
    by `author` (the same author iterates on its own candidate; the pointer
    moves as before). Otherwise a foreign candidate exists and the caller
    must refuse: `{candidate_version, author, saved_at}` — never replaced
    silently."""
    n = p.candidate_version
    if not n:
        return None
    row = await get_version_row(session, p, n)
    if row is None or row.author == author:
        return None
    saved_at = row.created_at.isoformat() if row.created_at is not None else None
    return {"candidate_version": n, "author": row.author, "saved_at": saved_at}


def conflict_message(name: str, conflict: dict) -> str:
    """The refusal sentence every guarded writer returns (edit and propose)."""
    return (
        f"Playbook '{name}' already has an unpublished candidate "
        f"v{conflict['candidate_version']} saved by "
        f"{author_label(conflict['author'])} at {conflict['saved_at']}. "
        "Nothing was saved — a candidate written by someone else is never "
        "replaced silently. Ask the owner whether to publish it or replace "
        "it; then re-read and retry."
    )


async def mint_version(
    session: AsyncSession,
    p: Playbook,
    *,
    definition: dict,
    code: str | None,
    manifest: str,
    author: str,
    message: str,
    promoted_from: int | None = None,
    format: str | None = None,
) -> PlaybookVersion:
    """Increment `p.version` and add the row for the new number. Callers
    decide what the number means (move `live_version` / `candidate_version`
    themselves) and commit. phase 08: `format` stamps the row's language;
    None = the playbook's current format."""
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
        format=format or p.format or "pblang",
    )
    session.add(row)
    # plans/022 P6: assert the snapshot row actually reached the DB — the
    # incident's "rowless v39" was a bumped counter with no content row,
    # reported as success. Flush surfaces constraint/write errors HERE.
    await session.flush()
    if row.id is None:
        raise RuntimeError(
            f"version mint failed: counter moved to {p.version} but the "
            "snapshot row was not written — aborting instead of leaving a "
            "rowless version"
        )
    return row


async def heal_duplicate_version_rows(session_factory) -> int:
    """0.38.0: delete redundant duplicate (playbook_id, version) rows.

    The pre-0.32 whole-source edit path snapshotted at the current counter even
    when that number already had a row, leaving e.g. two v33s — the Versions
    list showed both and version reads 500ed (MultipleResultsFound). Keeps
    the same row `get_version_row` prefers (plans/022 P6: content first —
    steps, then code, then manifest — then lineage, then oldest; see
    _dup_keep_key) and drops the rest, logging what was dropped and why.
    Idempotent; returns rows deleted."""
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
            keep = min(group, key=_dup_keep_key)
            for r in group:
                if r.id != keep.id:
                    await session.delete(r)
                    deleted += 1
                    log.info(
                        "playbooks: healing dropped duplicate v%d row %s "
                        "(playbook %s): steps=%s code=%s manifest=%s "
                        "created=%s — kept %s", n, r.id, pid,
                        bool(r.definition and r.definition.get("steps")),
                        bool(r.code), bool(r.manifest), r.created_at, keep.id,
                    )
        if deleted:
            await session.commit()
    return deleted

