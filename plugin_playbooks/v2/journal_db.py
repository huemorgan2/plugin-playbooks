"""Durable v2 journal store (plans/032 phase 06; docs/v2.md §6).

`DbJournalStore` implements `JournalStore` over the `playbook_journal` table
(`models.PlaybookJournal`). Every method opens ONE session and commits before
returning — `append_in_flight`'s commit is the write-ahead guarantee: a row
exists before the effect executes, so a process death between the row and
the result leaves an `in_flight` row a restart reconciles instead of
re-executing. The store never deletes a row (`drop` is a no-op; rows cascade
with the run row). `read()` returns exactly the entry dicts the memory store
returns, so the two stores are interchangeable for the shim.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from ..models import PlaybookJournal

# entry-0 fields live in row 0's `args`; these two are the row's own columns
_ENTRY0_ROW_KEYS = ("seq", "kind")
# kind-specific `extra` fields with their own column (phase 03)
_EXTRA_COLUMNS = ("cost_cents", "transcript", "child_run_id")


def _uuid(run_id: Any) -> uuid.UUID:
    return run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # sqlite returns naive datetimes; stored values are UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _parse(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DbJournalStore:
    """`JournalStore` over `playbook_journal` — one session per call, commit
    per call, never a delete."""

    def __init__(self, session_factory: Any) -> None:
        self._sf = session_factory

    # ------------------------------------------------------------ writes
    async def start(self, run_id: str, entry0: dict[str, Any]) -> None:
        args = {k: copy.deepcopy(v) for k, v in entry0.items() if k not in _ENTRY0_ROW_KEYS}
        async with self._sf() as session:
            session.add(PlaybookJournal(
                run_id=_uuid(run_id), seq=0, kind="run", status="done", args=args,
                idempotency_key=f"{run_id}:0", attempts=[], dry=bool(entry0.get("mode") == "dry"),
                started_at=_parse(entry0.get("started_at")) or _now(),
            ))
            await session.commit()

    async def append_in_flight(self, run_id: str, entry: dict[str, Any]) -> int:
        rid = _uuid(run_id)
        async with self._sf() as session:
            last = (await session.execute(
                select(func.max(PlaybookJournal.seq)).where(PlaybookJournal.run_id == rid)
            )).scalar_one()
            if last is None:
                raise KeyError(f"no journal for run {run_id}")
            seq = int(last) + 1
            session.add(PlaybookJournal(
                run_id=rid, seq=seq, kind=str(entry.get("kind")),
                call_site_id=entry.get("id"), occurrence=entry.get("occurrence"),
                name=entry.get("name"), args=copy.deepcopy(entry.get("args")),
                idempotency_key=f"{run_id}:{seq}", status="in_flight",
                result=None, error=None, attempts=[], dry=bool(entry.get("dry", False)),
                started_at=_parse(entry.get("started_at")) or _now(), ended_at=None, ms=None,
            ))
            await session.commit()
        return seq

    async def _get(self, session: Any, run_id: str, seq: int) -> PlaybookJournal:
        if int(seq) < 1:
            raise KeyError(f"run {run_id}: no journal entry seq={seq}")
        row = await session.get(PlaybookJournal, (_uuid(run_id), int(seq)))
        if row is None:
            raise KeyError(f"run {run_id}: no journal entry seq={seq}")
        return row

    @staticmethod
    def _apply_extra(row: PlaybookJournal, extra: dict[str, Any] | None) -> None:
        for key in _EXTRA_COLUMNS:
            if extra and key in extra:
                value = extra[key]
                if key == "child_run_id" and value is not None:
                    value = _uuid(value)
                setattr(row, key, copy.deepcopy(value))

    async def complete(
        self, run_id: str, seq: int, result: Any, attempts: list[dict[str, Any]], ms: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        async with self._sf() as session:
            row = await self._get(session, run_id, seq)
            row.status = "done"
            row.result = copy.deepcopy(result)
            row.error = None
            row.attempts = copy.deepcopy(list(attempts))
            row.ended_at = _now()
            row.ms = int(ms)
            self._apply_extra(row, extra)
            await session.commit()

    async def fail(
        self, run_id: str, seq: int, error_type: str, message: str,
        attempts: list[dict[str, Any]], extra: dict[str, Any] | None = None,
    ) -> None:
        async with self._sf() as session:
            row = await self._get(session, run_id, seq)
            row.status = "failed"
            row.error = {"type": error_type, "message": message}
            row.attempts = copy.deepcopy(list(attempts))
            row.ended_at = _now()
            self._apply_extra(row, extra)
            await session.commit()

    async def park(self, run_id: str, seq: int, parked_on: dict[str, Any]) -> None:
        # phase 07: the parking effect's row — `parked` is not `in_flight`, so a
        # restart's reconciliation (`SegmentLoop.resume`) leaves it alone.
        async with self._sf() as session:
            row = await self._get(session, run_id, seq)
            row.status = "parked"
            row.parked_on = copy.deepcopy(dict(parked_on))
            await session.commit()

    async def mark_handled(self, run_id: str, seqs: list[int]) -> None:
        # Risks 9: only `failed` rows are re-stamped — a handled OutcomeUnknown
        # keeps `timed_out_unknown` (the memory store does the same).
        if not seqs:
            return
        async with self._sf() as session:
            rows = (await session.execute(
                select(PlaybookJournal).where(
                    PlaybookJournal.run_id == _uuid(run_id),
                    PlaybookJournal.seq.in_([int(s) for s in seqs]),
                    PlaybookJournal.status == "failed",
                )
            )).scalars().all()
            for row in rows:
                if row.seq >= 1:
                    row.status = "failed_handled"
            if rows:
                await session.commit()

    async def mark_unknown(self, run_id: str, seq: int, message: str) -> None:
        async with self._sf() as session:
            row = await self._get(session, run_id, seq)
            row.status = "timed_out_unknown"
            row.error = {"type": "OutcomeUnknown", "message": message}
            row.ended_at = _now()
            await session.commit()

    async def drop(self, run_id: str) -> None:
        """A durable store never deletes on completion."""
        return None

    # ------------------------------------------------------------ reads
    @staticmethod
    def _entry(row: PlaybookJournal) -> dict[str, Any]:
        if row.seq == 0:
            e0: dict[str, Any] = {"seq": 0, "kind": "run"}
            e0.update(copy.deepcopy(row.args or {}))
            return e0
        entry: dict[str, Any] = {
            "seq": row.seq, "kind": row.kind, "id": row.call_site_id,
            "occurrence": row.occurrence, "name": row.name,
            "args": copy.deepcopy(row.args), "idempotency_key": row.idempotency_key,
            "status": row.status, "result": copy.deepcopy(row.result),
            "error": copy.deepcopy(row.error),
            "attempts": copy.deepcopy(row.attempts) if row.attempts is not None else [],
            "dry": bool(row.dry), "started_at": _iso(row.started_at),
            "ended_at": _iso(row.ended_at), "ms": row.ms,
        }
        # kind-specific fields only when present (the memory store's `extra` merge)
        if row.cost_cents is not None:
            entry["cost_cents"] = row.cost_cents
        if row.transcript is not None:
            entry["transcript"] = copy.deepcopy(row.transcript)
        if row.child_run_id is not None:
            entry["child_run_id"] = str(row.child_run_id)
        if row.parked_on is not None:
            entry["parked_on"] = copy.deepcopy(row.parked_on)
        return entry

    async def _rows(self, run_id: str, *, status: str | None = None) -> list[PlaybookJournal]:
        stmt = select(PlaybookJournal).where(PlaybookJournal.run_id == _uuid(run_id))
        if status is not None:
            stmt = stmt.where(PlaybookJournal.status == status, PlaybookJournal.seq >= 1)
        stmt = stmt.order_by(PlaybookJournal.seq)
        async with self._sf() as session:
            return list((await session.execute(stmt)).scalars().all())

    async def read(self, run_id: str) -> list[dict[str, Any]]:
        rows = await self._rows(run_id)
        if not rows:
            raise KeyError(f"no journal for run {run_id}")
        return [self._entry(r) for r in rows]

    async def entry0(self, run_id: str) -> dict[str, Any] | None:
        async with self._sf() as session:
            row = await session.get(PlaybookJournal, (_uuid(run_id), 0))
        return self._entry(row) if row is not None else None

    async def in_flight(self, run_id: str) -> list[dict[str, Any]]:
        return [self._entry(r) for r in await self._rows(run_id, status="in_flight")]

    async def journaled(self, run_ids: list[Any]) -> set[Any]:
        ids = list(run_ids)
        if not ids:
            return set()
        by_uuid = {_uuid(rid): rid for rid in ids}
        async with self._sf() as session:
            found = (await session.execute(
                select(PlaybookJournal.run_id).where(
                    PlaybookJournal.seq == 0,
                    PlaybookJournal.run_id.in_(list(by_uuid)),
                )
            )).scalars().all()
        return {by_uuid[_uuid(f)] for f in found if _uuid(f) in by_uuid}
