"""v2 journal store (plans/032 phase 02; docs/v2.md §6).

`JournalStore` is the interface the segment loop writes through and the shim
replays from; `MemoryJournalStore` is the in-memory implementation (dry runs
and unit tests) and `journal_db.DbJournalStore` (phase 06) the durable one
behind the same Protocol. Entry shapes are fixed by docs/v2.md §6 — entry 0
describes the run, every later entry is one effect occurrence, written
`in_flight` BEFORE the effect executes.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Protocol


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JournalStore(Protocol):
    """Per-run ordered journal. Every method is awaited by the loop."""

    async def start(self, run_id: str, entry0: dict[str, Any]) -> None: ...

    async def append_in_flight(self, run_id: str, entry: dict[str, Any]) -> int: ...

    async def complete(
        self, run_id: str, seq: int, result: Any, attempts: list[dict[str, Any]], ms: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """`extra` (phase 03) merges kind-specific fields onto the row:
        `cost_cents` (llm/agent), `transcript` (agent), `child_run_id` (subtask)."""
        ...

    async def fail(
        self, run_id: str, seq: int, error_type: str, message: str,
        attempts: list[dict[str, Any]], extra: dict[str, Any] | None = None,
    ) -> None: ...

    async def mark_handled(self, run_id: str, seqs: list[int]) -> None:
        """Re-stamp `failed` rows the code caught and proceeded past as
        `failed_handled` (phase 03; docs/v2.md §2/§6)."""
        ...

    async def read(self, run_id: str) -> list[dict[str, Any]]: ...

    async def entry0(self, run_id: str) -> dict[str, Any] | None: ...

    async def drop(self, run_id: str) -> None:
        """Release the run's journal once the run is terminal (memory store);
        durable stores keep it and treat this as a no-op."""
        ...

    async def in_flight(self, run_id: str) -> list[dict[str, Any]]:
        """Phase 06: the run's `in_flight` rows in seq order — what a restart
        finds and `SegmentLoop.resume` reconciles."""
        ...

    async def mark_unknown(self, run_id: str, seq: int, message: str) -> None:
        """Phase 06: status `timed_out_unknown`, `error={"type": "OutcomeUnknown",
        "message": message}`, `ended_at` — the row is never re-executed."""
        ...

    async def journaled(self, run_ids: list[Any]) -> set[Any]:
        """Phase 06: the subset of `run_ids` that have a row 0 — the v2
        marker the sweep skips and the resume scan selects. Ids are returned
        as given (the caller's type)."""
        ...


def make_entry0(
    *, hash_seed: int, inputs: dict[str, Any], playbook: str, version: int,
    max_effects: int, mode: str = "real", fmt: str = "python",
    code_sha256: str | None = None,
) -> dict[str, Any]:
    """Entry 0 (docs/v2.md §6). `code_sha256` (phase 06) pins the source the
    run started on so a resume detects "code edited under a run" before any
    jail spawn."""
    entry = {
        "seq": 0, "kind": "run", "hash_seed": hash_seed, "inputs": inputs,
        "playbook": playbook, "version": version, "format": fmt, "mode": mode,
        "max_effects": max_effects, "started_at": _now_iso(),
    }
    if code_sha256 is not None:
        entry["code_sha256"] = code_sha256
    return entry


def make_effect_entry(
    *, run_id: str, seq: int, kind: str, id: str, occurrence: int,
    name: str | None, args: Any, dry: bool = False,
) -> dict[str, Any]:
    """An `in_flight` row (docs/v2.md §6); `seq` is assigned by the caller."""
    return {
        "seq": seq, "kind": kind, "id": id, "occurrence": occurrence, "name": name,
        "args": args, "idempotency_key": f"{run_id}:{seq}", "status": "in_flight",
        "result": None, "error": None, "attempts": [], "dry": dry,
        "started_at": _now_iso(), "ended_at": None, "ms": None,
    }


class MemoryJournalStore:
    """dict per run_id; dropped when the run completes unless
    `keep_completed` (tests read the journal after the run)."""

    def __init__(self, *, keep_completed: bool = False) -> None:
        self._runs: dict[str, list[dict[str, Any]]] = {}
        self.keep_completed = keep_completed

    def _entries(self, run_id: str) -> list[dict[str, Any]]:
        try:
            return self._runs[run_id]
        except KeyError:
            raise KeyError(f"no journal for run {run_id}") from None

    async def start(self, run_id: str, entry0: dict[str, Any]) -> None:
        e0 = copy.deepcopy(entry0)
        e0["seq"] = 0
        e0.setdefault("kind", "run")
        self._runs[run_id] = [e0]

    async def append_in_flight(self, run_id: str, entry: dict[str, Any]) -> int:
        entries = self._entries(run_id)
        seq = len(entries)
        row = copy.deepcopy(entry)
        row["seq"] = seq
        row["idempotency_key"] = f"{run_id}:{seq}"
        row["status"] = "in_flight"
        entries.append(row)
        return seq

    def _row(self, run_id: str, seq: int) -> dict[str, Any]:
        entries = self._entries(run_id)
        if seq < 1 or seq >= len(entries):
            raise KeyError(f"run {run_id}: no journal entry seq={seq}")
        return entries[seq]

    async def complete(
        self, run_id: str, seq: int, result: Any, attempts: list[dict[str, Any]], ms: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        row = self._row(run_id, seq)
        row["status"] = "done"
        row["result"] = copy.deepcopy(result)
        row["error"] = None
        row["attempts"] = copy.deepcopy(list(attempts))
        row["ended_at"] = _now_iso()
        row["ms"] = int(ms)
        if extra:
            row.update(copy.deepcopy(dict(extra)))

    async def fail(
        self, run_id: str, seq: int, error_type: str, message: str,
        attempts: list[dict[str, Any]], extra: dict[str, Any] | None = None,
    ) -> None:
        row = self._row(run_id, seq)
        row["status"] = "failed"
        row["error"] = {"type": error_type, "message": message}
        row["attempts"] = copy.deepcopy(list(attempts))
        row["ended_at"] = _now_iso()
        if extra:
            row.update(copy.deepcopy(dict(extra)))

    async def mark_handled(self, run_id: str, seqs: list[int]) -> None:
        entries = self._runs.get(run_id) or []
        for seq in seqs:
            if 1 <= int(seq) < len(entries) and entries[int(seq)].get("status") == "failed":
                entries[int(seq)]["status"] = "failed_handled"

    async def read(self, run_id: str) -> list[dict[str, Any]]:
        return copy.deepcopy(self._entries(run_id))

    async def entry0(self, run_id: str) -> dict[str, Any] | None:
        entries = self._runs.get(run_id)
        return copy.deepcopy(entries[0]) if entries else None

    async def drop(self, run_id: str) -> None:
        if not self.keep_completed:
            self._runs.pop(run_id, None)

    async def in_flight(self, run_id: str) -> list[dict[str, Any]]:
        entries = self._runs.get(run_id) or []
        return [copy.deepcopy(e) for e in entries[1:] if e.get("status") == "in_flight"]

    async def mark_unknown(self, run_id: str, seq: int, message: str) -> None:
        row = self._row(run_id, seq)
        row["status"] = "timed_out_unknown"
        row["error"] = {"type": "OutcomeUnknown", "message": message}
        row["ended_at"] = _now_iso()

    async def journaled(self, run_ids: list[Any]) -> set[Any]:
        return {rid for rid in run_ids if str(rid) in self._runs}
