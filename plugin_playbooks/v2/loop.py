"""Host segment loop for v2 playbooks (plans/032 phase 02; docs/v2.md §6, §7, §11).

`SegmentLoop.drive(run, playbook, inputs)` runs a v2 playbook end to end on
segmented replay: it writes entry 0, then repeatedly invokes the jail
(`code_run` with `SHIM_SOURCE` and the envelope), dispatches on the shim's
`kind` — `effect`: journal `in_flight` FIRST, execute with v1 parity, complete
or fail the row, re-invoke; `gather`: the same for a batch (phase 03 emits
it); `return`: done; `error`: the run fails with the four run columns.
`SegmentLoop.resume(run, playbook)` (phase 06) continues a `running` run from
its durable journal after a process death: in-flight rows are reconciled
(`timed_out_unknown` or re-executed in place) and the same segment body runs
on the untouched journal prefix.

Runner internals (`_normalize_tool_result`, `_active_run_id`, vault
resolution, the step-row shapes) are imported lazily inside functions —
`runner.py` imports this package at module level.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from random import Random, SystemRandom
from typing import Any

from . import DEFAULT_TIMEOUTS as _DEFAULT_TIMEOUTS
from . import MAX_EFFECTS
from .dry import DRY_BANNER, dry_answer
from .journal import JournalStore, MemoryJournalStore, make_effect_entry, make_entry0
from .shim import SHIM_SOURCE

log = logging.getLogger("luna.playbooks.v2")

# Module-level so a test can `monkeypatch.setattr("plugin_playbooks.v2.loop.DEFAULT_TIMEOUTS", ...)`;
# read at call time, never bound at import.
DEFAULT_TIMEOUTS: dict[str, float | None] = dict(_DEFAULT_TIMEOUTS)

_STDERR_TAIL = 2000
_RETRY_BACKOFF_CAP = 60.0


@dataclass
class LoopResult:
    value: Any = None
    segments: int = 0
    # (host_ms, jail_ms) per segment — master §5 "jail spawn latency" baseline
    segment_latency_ms: list[tuple[int, int]] = field(default_factory=list)
    # phase 07: the run parked on `ctx.approve` / `ctx.wait_event` — no value,
    # the row is `parked`, `ParkService` resumes it later
    parked: bool = False


class V2RunError(Exception):
    """A v2 run ended `failed`: carries the four run columns (docs/v2.md §7).
    Raised out of `drive()`; `_drive_run` turns it into `_complete_run`."""

    def __init__(
        self, error: str, error_type: str, traceback: str | None = None,
        failed_at: datetime | None = None,
    ) -> None:
        super().__init__(error)
        self.error = error
        self.error_type = error_type
        self.traceback = traceback
        self.failed_at = failed_at or datetime.now(timezone.utc)


class _EffectFailure(Exception):
    """Host-side effect failure with the journal `error.type`; `extra` are
    kind-specific fields merged onto the failed row (e.g. `child_run_id`)."""

    error_type = "EffectError"

    def __init__(self, message: str, extra: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.extra = extra


class _ToolError(_EffectFailure):
    error_type = "ToolError"


class _EffectTimeout(_EffectFailure):
    error_type = "EffectTimeout"


class _Rejected(_EffectFailure):
    error_type = "Rejected"


class _ApprovalExpired(_EffectFailure):
    error_type = "ApprovalExpired"


class _SubtaskFailed(_EffectFailure):
    error_type = "SubtaskFailed"


class _EventTimeout(_EffectFailure):
    error_type = "EventTimeout"


class _Parked(Exception):
    """Phase 07 signal (not a failure): the effect parked the run. Carries
    the `parked_on` the service wrote; the loop stops driving segments."""

    def __init__(self, parked_on: dict[str, Any]) -> None:
        super().__init__(parked_on.get("kind", "parked"))
        self.parked_on = parked_on


_RETRYABLE = (_ToolError, _EffectTimeout)

# Phase 07: the kinds that may park the run. In a gather they run after the
# other members, sequentially, so the first park leaves the rest untouched.
_PARK_KINDS = frozenset({"approve", "wait_event"})

# Phase 06 (docs/v2.md §6): the kinds a resume re-executes in place from the
# journaled args — side-effect-free outside the journal. Every other kind found
# `in_flight` after a restart becomes `timed_out_unknown` and is never re-run.
_REEXECUTE_ON_RESUME = frozenset({"llm", "now", "random", "log"})


def code_sha256(source: str) -> str:
    """The entry-0 `code_sha256` pin (phase 06): the source a run started on."""
    return hashlib.sha256((source or "").encode("utf-8")).hexdigest()


@dataclass
class _RunState:
    """Per-run loop state (phase 03): the playbook identity `ctx.approve`
    stamps on its card and the ancestor playbook-name chain the subtask
    cycle guard checks (parent chain + own name)."""

    name: str
    version: int
    chain: list[str]


@dataclass
class _DryRun:
    """The transient run a dry drive uses instead of a `PlaybookRun` row
    (phase 05): a uuid4 id for the envelope and journal, nothing persisted."""

    id: uuid.UUID
    playbook_version: int
    is_test: bool = True
    report_to: Any = None
    parent_run_id: Any = None


def _parse_retry(retry: Any) -> tuple[int, float]:
    """`_retry` → (extra attempts, backoff base seconds); docs/v2.md §3/§6."""
    if retry is None or retry is False:
        return 0, 1.0
    if isinstance(retry, bool):
        return (1, 1.0) if retry else (0, 1.0)
    if isinstance(retry, int):
        return max(0, retry), 1.0
    if isinstance(retry, dict):
        try:
            attempts = max(0, int(retry.get("attempts", 0)))
        except (TypeError, ValueError):
            attempts = 0
        try:
            backoff = float(retry.get("backoff", 1.0))
        except (TypeError, ValueError):
            backoff = 1.0
        return attempts, max(0.0, backoff)
    return 0, 1.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def one_liner(
    *, source: str, error_type: str, message: str, playbook_line: int | None,
    last_completed_effect: dict[str, Any] | None,
) -> str:
    """`line <n>: <source line> → <error_type>: <message> after effect <id>#<n>`
    (docs/v2.md §7); no `line` prefix when no playbook frame is on the stack."""
    if last_completed_effect and last_completed_effect.get("id"):
        suffix = f"after effect {last_completed_effect['id']}"
    else:
        suffix = "before any effect"
    head = f"{error_type}: {message}".rstrip(": ") if message else error_type
    if playbook_line:
        lines = (source or "").splitlines()
        src = lines[playbook_line - 1].strip() if 0 < playbook_line <= len(lines) else ""
        return f"line {playbook_line}: {src} → {head} {suffix}"
    return f"{head} {suffix}"


def format_traceback(filename: str, frames: list[dict[str, Any]], error_type: str, message: str) -> str:
    out = ["Traceback (playbook frames only):"]
    for fr in frames or []:
        out.append(f'  File "{filename}", line {fr.get("line")}, in {fr.get("name") or "run"}')
        if fr.get("source"):
            out.append(f"    {fr['source']}")
    out.append(f"{error_type}: {message}" if message else error_type)
    return "\n".join(out)


class SegmentLoop:
    """Drives v2 runs; one instance per `PlaybookRunner`."""

    def __init__(
        self, session_factory: Any, tools: Any, events: Any, ctx: Any,
        journal: JournalStore, *, segment_timeout: int = 60,
        max_effects: int = MAX_EFFECTS, agent: Any = None, start_run: Any = None,
        mode: str = "live", stubs: dict[str, Any] | None = None, park: Any = None,
    ) -> None:
        if mode not in ("live", "dry"):
            raise ValueError(f"SegmentLoop mode must be 'live' or 'dry', got {mode!r}")
        self._sf = session_factory
        # phase 07: the `ParkService` (`park_approval` / `park_event`); None
        # means no parking — `ctx.approve` blocks in-process (phase 03 form)
        # and `ctx.wait_event` fails.
        self.park = park
        self._tools = tools
        self._events = events
        self._ctx = ctx
        # phase 03: the PluginAgent facade v1 uses (`run_llm`/`run_turn`) and
        # the bound `PlaybookRunner.start_run` that `ctx.subtask` goes through.
        self._agent = agent
        self._start_run = start_run
        self.journal = journal
        self.segment_timeout = segment_timeout
        self.max_effects = max_effects
        # phase 05 (docs/v2.md §10): in dry mode every effect is answered by
        # `dry_answer` from `stubs`, every journal row carries `dry: true`,
        # and no run/step row or step event is written.
        self.mode = mode
        self._stubs: dict[str, Any] = dict(stubs or {})
        self._dry_rng: dict[str, Random] = {}
        self.last_result: LoopResult | None = None
        # phase 03: `run()` return values of finished CHILD runs, keyed by run
        # id — `start_run` hands back the row, not the value; the parent pops
        # its child's value here (one loop per runner, so both share it).
        self._values: dict[str, Any] = {}
        self._runs: dict[str, _RunState] = {}

    @property
    def dry(self) -> bool:
        return self.mode == "dry"

    # ------------------------------------------------------------ dry run
    async def dry_run(
        self, playbook: Any, inputs: dict[str, Any] | None = None,
        stubs: dict[str, Any] | None = None, *, version: int | None = None,
    ) -> dict[str, Any]:
        """Dry-run `playbook` (docs/v2.md §10) on a sibling loop in dry mode
        with its own throwaway `MemoryJournalStore`. Intake coercion runs
        first (`InputTypeError` propagates: a bad input fails before segment
        1). Returns the result dict: `status` (`simulated` /
        `simulated_nothing_exercised`), `dry_run`, `banner`, `steps_ran`,
        `unreached_call_sites`, `journal`, `result`, `error`, `error_type`."""
        from ..runner import _coerce_inputs
        from .checker import check

        coerced = _coerce_inputs(playbook, dict(inputs or {}))
        loop = SegmentLoop(
            self._sf, self._tools, self._events, self._ctx,
            MemoryJournalStore(keep_completed=True),
            segment_timeout=self.segment_timeout, max_effects=self.max_effects,
            agent=self._agent, start_run=self._start_run, mode="dry", stubs=stubs,
        )
        version_n = int(version or getattr(playbook, "live_version", 0) or 1)
        run = _DryRun(id=uuid.uuid4(), playbook_version=version_n)
        name = playbook.name
        call_sites = check(playbook.code or "", name=name, version=version_n).summary.get("call_sites", [])
        error = error_type = None
        value = None
        try:
            value = (await loop.drive(run, playbook, coerced)).value
        except V2RunError as e:
            error, error_type = e.error, e.error_type
        journal = await loop.journal.read(str(run.id))
        effects = journal[1:]
        steps_ran = {
            f"{e['id']}#{e['occurrence']}": {
                "kind": e.get("kind"), "args": e.get("args"), "result": e.get("result"),
                "stubbed": bool(e.get("stubbed")),
            }
            for e in effects
        }
        reached = {e["id"] for e in effects}
        unreached = [
            {"id": s["id"], "kind": s.get("kind"), "line": s.get("line")}
            for s in call_sites if s.get("id") not in reached
        ]
        return {
            "status": "simulated" if effects else "simulated_nothing_exercised",
            "dry_run": True,
            "banner": DRY_BANNER,
            "steps_ran": steps_ran,
            "unreached_call_sites": unreached,
            "journal": journal,
            "result": value,
            "error": error,
            "error_type": error_type,
        }

    # ------------------------------------------------------------ drive
    async def drive(self, run: Any, playbook: Any, inputs: dict[str, Any]) -> LoopResult:
        from .checker import check

        run_id = str(run.id)
        name = playbook.name
        version = int(getattr(run, "playbook_version", None) or getattr(playbook, "live_version", 0) or 1)
        source = playbook.code or ""
        filename = f"playbook:{name}@v{version}"
        call_sites = check(source, name=name, version=version).summary.get("call_sites", [])
        # dry: fixed seeds (docs/v2.md §10) — two dry runs of one version agree
        hash_seed = 0 if self.dry else secrets.randbelow(2**32)
        await self.journal.start(run_id, make_entry0(
            hash_seed=hash_seed, inputs=dict(inputs or {}), playbook=name, version=version,
            max_effects=self.max_effects, mode="dry" if self.dry else "real",
            code_sha256=code_sha256(source),
        ))
        if self.dry:
            self._dry_rng[run_id] = Random(0)
        result = LoopResult()
        parent_id = getattr(run, "parent_run_id", None)
        parent_chain = self._runs[str(parent_id)].chain if parent_id is not None and str(parent_id) in self._runs else []
        self._runs[run_id] = _RunState(name=name, version=version, chain=[*parent_chain, name])
        try:
            return await self._segments(
                run, result, name=name, version=version, source=source, filename=filename,
                call_sites=call_sites, hash_seed=hash_seed, max_effects=self.max_effects,
                parent_id=parent_id,
            )
        finally:
            # the most recently FINISHED drive: a subtask's child finishes
            # before its parent, so the parent's result is what stays here
            self.last_result = result
            self._runs.pop(run_id, None)
            self._dry_rng.pop(run_id, None)
            await self.journal.drop(run_id)

    # ------------------------------------------------------------ resume
    async def resume(self, run: Any, playbook: Any, *, from_park: bool = False) -> LoopResult:
        """Phase 06 (docs/v2.md §6): continue a `running` run from its durable
        journal after a process death. No seed, no entry 0 — `hash_seed`,
        `inputs` and `max_effects` come from the journal; the ancestor chain
        is rebuilt from `playbook_runs.parent_run_id`; the run's `in_flight`
        rows are reconciled (tool/agent/subtask/approve/wait_event →
        `timed_out_unknown`, llm/now/random/log re-executed in place) and then
        the segment body continues exactly as `drive()` would have — same
        journal prefix.

        Phase 07 `from_park=True`: the same continuation after a park was
        resolved (`ParkService._resume` completed/failed the parking entry).
        The process did not die, so `in_flight` approve/wait_event rows are
        gather members that were never started — they are re-executed (and
        may park the run again) instead of going `timed_out_unknown`."""
        from .checker import check

        run_id = str(run.id)
        e0 = await self.journal.entry0(run_id)
        if e0 is None:
            raise V2RunError(
                f"run {run_id} has no journal — it cannot be resumed", "JournalDivergence",
            )
        name = playbook.name
        version = int(getattr(run, "playbook_version", None) or e0.get("version") or 1)
        source = playbook.code or ""
        filename = f"playbook:{name}@v{version}"
        call_sites = check(source, name=name, version=version).summary.get("call_sites", [])
        hash_seed = int(e0.get("hash_seed") or 0)
        max_effects = int(e0.get("max_effects") or self.max_effects)
        result = LoopResult()
        parent_id = getattr(run, "parent_run_id", None)
        chain = await self._ancestor_chain(run)
        self._runs[run_id] = _RunState(name=name, version=version, chain=[*chain, name])
        try:
            if await self._reconcile(run, from_park=from_park):
                result.parked = True
                return result
            return await self._segments(
                run, result, name=name, version=version, source=source, filename=filename,
                call_sites=call_sites, hash_seed=hash_seed, max_effects=max_effects,
                parent_id=parent_id,
            )
        finally:
            self.last_result = result
            self._runs.pop(run_id, None)
            await self.journal.drop(run_id)

    async def _ancestor_chain(self, run: Any) -> list[str]:
        """The ancestor playbook-name chain (oldest first) walked up
        `parent_run_id`: a live parent's in-memory state when present, the
        run/playbook rows otherwise (phase 03 kept the chain in memory only)."""
        from sqlalchemy import select

        from ..models import Playbook, PlaybookRun

        parent_id = getattr(run, "parent_run_id", None)
        if parent_id is None:
            return []
        if str(parent_id) in self._runs:
            return list(self._runs[str(parent_id)].chain)
        chain: list[str] = []
        seen: set[str] = set()
        async with self._sf() as session:
            cursor = parent_id
            while cursor is not None and str(cursor) not in seen and len(chain) < 64:
                seen.add(str(cursor))
                parent = await session.get(PlaybookRun, cursor)
                if parent is None:
                    break
                pname = (await session.execute(
                    select(Playbook.name).where(Playbook.id == parent.playbook_id)
                )).scalar_one_or_none()
                if pname:
                    chain.append(pname)
                cursor = parent.parent_run_id
        chain.reverse()
        return chain

    async def _reconcile(self, run: Any, *, from_park: bool = False) -> bool:
        """Reconciliation before the first resumed segment (docs/v2.md §6), in
        seq order over the run's `in_flight` rows. Returns True when a
        re-executed park-kind row parked the run again (phase 07)."""
        run_id = str(run.id)
        for row in await self.journal.in_flight(run_id):
            seq = int(row["seq"])
            kind = str(row.get("kind"))
            key = f"{row.get('id')}#{row.get('occurrence')}"
            if from_park and kind in _PARK_KINDS:
                # phase 07: a gather sibling of the resolved park that was
                # never started (the loop parks on the first one). The
                # journaled `options` are not stored, so a `_timeout` set on
                # the call is lost here — the effect runs with its default.
                eff = {
                    "seq": seq, "id": key, "call_site_id": row.get("id"),
                    "occurrence": row.get("occurrence"), "effect_kind": kind,
                    "name": row.get("name"),
                    "args": row.get("args") if isinstance(row.get("args"), dict) else {},
                    "options": {},
                }
                step_run_id = await self._running_step(run.id, key)
                if step_run_id is None:
                    step_run_id = await self._create_step(run.id, key, kind)
                log.info("playbook.v2.resume.park_sibling run_id=%s seq=%d effect=%s", run_id, seq, key)
                if await self._execute_and_finish(run, eff, seq, step_run_id):
                    return True
                continue
            if kind in _REEXECUTE_ON_RESUME:
                # side-effect-free: re-executed by the host from the journaled
                # args into the SAME row — seq/idempotency_key unchanged, so a
                # gather batch keeps its seq alignment (Risks 3, 15)
                eff = {
                    "seq": seq, "id": key, "call_site_id": row.get("id"),
                    "occurrence": row.get("occurrence"), "effect_kind": kind,
                    "name": row.get("name"),
                    "args": row.get("args") if isinstance(row.get("args"), dict) else {},
                    "options": {},
                }
                step_run_id = await self._running_step(run.id, key)
                if step_run_id is None:
                    step_run_id = await self._create_step(run.id, key, kind)
                log.info("playbook.v2.resume.reexecute run_id=%s seq=%d effect=%s", run_id, seq, key)
                await self._execute_and_finish(run, eff, seq, step_run_id)
                continue
            message = f"outcome unknown — the server restarted while effect {key} was in flight"
            await self.journal.mark_unknown(run_id, seq, message)
            step_run_id = await self._running_step(run.id, key)
            if step_run_id is not None:
                # Risks 12: the step row closes `failed` with the same message;
                # the journal row keeps the precise status
                await self._complete_step(step_run_id, "failed", error=f"OutcomeUnknown: {message}")
            await self._events.emit("playbook.step.failed", {
                "run_id": run_id, "step_id": key, "error": f"OutcomeUnknown: {message}",
                "retry_count": 0,
            })
            log.info("playbook.v2.resume.unknown run_id=%s seq=%d effect=%s", run_id, seq, key)
        return False

    async def _running_step(self, run_id: Any, step_id: str) -> Any:
        from sqlalchemy import select

        from ..models import PlaybookStepRun

        async with self._sf() as session:
            rows = (await session.execute(
                select(PlaybookStepRun).where(
                    PlaybookStepRun.run_id == run_id,
                    PlaybookStepRun.step_id == step_id[:128],
                    PlaybookStepRun.status == "running",
                ).order_by(PlaybookStepRun.started_at.desc())
            )).scalars().all()
        return rows[0].id if rows else None

    # ------------------------------------------------------------ segment body
    async def _segments(
        self, run: Any, result: LoopResult, *, name: str, version: int, source: str,
        filename: str, call_sites: list[Any], hash_seed: int, max_effects: int,
        parent_id: Any,
    ) -> LoopResult:
        run_id = str(run.id)
        rt = self._code_run_tool()
        while True:
            result.segments += 1
            n = result.segments
            journal = await self.journal.read(run_id)
            envelope = {
                "playbook": name, "version": version, "source": source,
                "hash_seed": hash_seed, "max_effects": max_effects,
                "call_sites": call_sites, "journal": journal,
            }
            payload, host_ms = await self._segment(rt, envelope, name, n)
            jail_ms = int(payload.get("duration_ms") or 0)
            result.segment_latency_ms.append((host_ms, jail_ms))
            log.info(
                "playbook.v2.segment run_id=%s n=%d host_ms=%d jail_ms=%d backend=%s",
                run_id, n, host_ms, jail_ms, payload.get("backend"),
            )
            self._check_payload(payload, journal, n)
            res = payload.get("result")
            if not isinstance(res, dict) or "kind" not in res:
                raise V2RunError(
                    f"segment {n}: the shim returned no result kind", "ShimFailure",
                )
            kind = res["kind"]
            handled = res.get("handled")
            if isinstance(handled, list) and handled:
                # phase 03: replayed failures the code caught and proceeded past
                await self.journal.mark_handled(run_id, [int(s) for s in handled])
            if kind == "return":
                result.value = res.get("value")
                if parent_id is not None:
                    self._values[run_id] = result.value
                return result
            if kind == "error":
                raise self._run_error(res, source, filename)
            if kind == "effect":
                if await self._effect(run, res):
                    result.parked = True
                    return result
                continue
            if kind == "gather":
                if await self._gather(run, res):
                    result.parked = True
                    return result
                continue
            raise V2RunError(
                f"segment {n}: unknown result kind {kind!r}", "ShimFailure",
            )

    # ------------------------------------------------------------ segments
    def _code_run_tool(self) -> Any:
        try:
            return self._tools.get("code_run")
        except KeyError:
            raise V2RunError(
                "v2 playbooks need plugin-inline-code-run (tool 'code_run') "
                "installed on this agent — it is not in the tool registry.",
                "ShimFailure",
            ) from None

    async def _segment(self, rt: Any, envelope: dict[str, Any], name: str, n: int) -> tuple[dict[str, Any], int]:
        from ..runner import _normalize_tool_result

        t0 = time.monotonic()
        raw = await rt.handler(
            code=SHIM_SOURCE, input_json=envelope, timeout_sec=self.segment_timeout,
            title=f"playbook '{name}' segment {n}",
        )
        host_ms = int((time.monotonic() - t0) * 1000)
        payload = _normalize_tool_result(raw)
        if not isinstance(payload, dict):
            raise V2RunError(
                f"segment {n}: code_run returned an unexpected result", "ShimFailure",
            )
        return payload, host_ms

    def _check_payload(self, payload: dict[str, Any], journal: list[dict[str, Any]], n: int) -> None:
        if not payload.get("ok"):
            if payload.get("timed_out"):
                if len(journal) > 1:
                    last = journal[-1]
                    where = f"after effect {last['id']}#{last['occurrence']}"
                else:
                    where = "before any effect"
                phase = (payload.get("progress") or {}).get("phase") if isinstance(payload.get("progress"), dict) else None
                detail = {
                    "replaying": "while replaying the journal",
                    "effect_exit": "at the effect exit",
                }.get(phase, "in pure compute")
                raise V2RunError(
                    f"segment {n} timed out ({self.segment_timeout}s) {where}, {detail}",
                    "SegmentTimeout",
                )
            detail = str(payload.get("error") or payload.get("stderr") or "").strip()[-_STDERR_TAIL:]
            raise V2RunError(
                f"segment {n} failed (exit {payload.get('exit_code')}): {detail}",
                "ShimFailure",
            )
        if "result_error" in payload:
            raise V2RunError(f"segment {n}: {payload['result_error']}", "ShimFailure")

    def _run_error(self, res: dict[str, Any], source: str, filename: str) -> V2RunError:
        error_type = str(res.get("error_type") or "Error")[:64]
        message = str(res.get("message") or "")
        frames = res.get("traceback") or []
        return V2RunError(
            one_liner(
                source=source, error_type=error_type, message=message,
                playbook_line=res.get("playbook_line") or 0,
                last_completed_effect=res.get("last_completed_effect"),
            ),
            error_type,
            traceback=format_traceback(filename, frames, error_type, message),
        )

    # ------------------------------------------------------------ effects
    async def _gather(self, run: Any, res: dict[str, Any]) -> bool:
        """Batching hook (phase 03 emits `gather`): every effect is journaled
        `in_flight` in argument order, then executed concurrently; every row
        is completed/failed before the next segment.

        Phase 07: park-kind members (`approve`, `wait_event`) run AFTER the
        others, one at a time — the first that parks returns True and leaves
        its later siblings `in_flight` for `resume(from_park=True)`."""
        effects = list(res.get("effects") or [])
        prepared = [await self._journal_and_start(run, eff) for eff in effects]
        parking = self.park is not None and not self.dry
        now = [p for p in prepared if not (parking and p[0].get("effect_kind") in _PARK_KINDS)]
        later = [p for p in prepared if parking and p[0].get("effect_kind") in _PARK_KINDS]
        # phase 03: every element settles (each journals its own outcome);
        # the first host-side exception, in argument order, is raised after.
        settled = await asyncio.gather(
            *(self._execute_and_finish(run, eff, seq, step_run_id) for eff, seq, step_run_id in now),
            return_exceptions=True,
        )
        for outcome in settled:
            if isinstance(outcome, BaseException):
                raise outcome
        for eff, seq, step_run_id in later:
            if await self._execute_and_finish(run, eff, seq, step_run_id):
                return True
        return False

    async def _effect(self, run: Any, eff: dict[str, Any]) -> bool:
        eff, seq, step_run_id = await self._journal_and_start(run, eff)
        return await self._execute_and_finish(run, eff, seq, step_run_id)

    async def _journal_and_start(self, run: Any, eff: dict[str, Any]) -> tuple[dict[str, Any], int, Any]:
        run_id = str(run.id)
        kind = eff.get("effect_kind")
        key = str(eff.get("id"))
        site_id, occurrence = self._split_key(eff, key)
        args = eff.get("args") if isinstance(eff.get("args"), dict) else {}
        # journal FIRST (docs/v2.md §6) — the row exists before anything runs
        seq = await self.journal.append_in_flight(run_id, make_effect_entry(
            run_id=run_id, seq=int(eff.get("seq") or 0), kind=kind, id=site_id,
            occurrence=occurrence, name=eff.get("name"), args=args, dry=self.dry,
        ))
        if eff.get("seq") is not None and int(eff["seq"]) != seq:
            await self.journal.fail(run_id, seq, "JournalDivergence", "seq mismatch", [])
            raise V2RunError(
                f"the shim asked for effect seq {eff['seq']} but the journal is at {seq}: "
                "code edited under a run, or non-journaled randomness",
                "JournalDivergence",
            )
        if self.dry:
            # no step row, no step event (docs/v2.md §10)
            return eff, seq, None
        try:
            step_run_id = await self._create_step(run.id, key, kind)
            await self._events.emit("playbook.step.started", {
                "run_id": run_id, "step_id": key, "step_kind": kind,
            })
        except asyncio.CancelledError:
            # a cancel landing between the journal row and the effect body
            # (phase 03: a subtask deadline cancelling its child) must not
            # leave the row `in_flight`
            with contextlib.suppress(Exception):
                await self.journal.fail(run_id, seq, "RunCancelled", "run cancelled", [])
            raise
        return eff, seq, step_run_id

    @staticmethod
    def _split_key(eff: dict[str, Any], key: str) -> tuple[str, int]:
        if eff.get("call_site_id") and eff.get("occurrence"):
            return str(eff["call_site_id"]), int(eff["occurrence"])
        site_id, _, occ = key.rpartition("#")
        try:
            return (site_id or key), int(occ)
        except ValueError:
            return key, 1

    async def _execute_and_finish(self, run: Any, eff: dict[str, Any], seq: int, step_run_id: Any) -> bool:
        """Run one journaled effect to its row's final status. Returns True
        when the effect PARKED the run (phase 07): the row is `parked`, the
        step row stays `running`, `playbook.run.parked` was emitted."""
        from ..runner import _active_run_id

        run_id = str(run.id)
        kind = eff.get("effect_kind")
        key = str(eff.get("id"))
        args = eff.get("args") if isinstance(eff.get("args"), dict) else {}
        options = eff.get("options") if isinstance(eff.get("options"), dict) else {}
        extra, backoff = _parse_retry(options.get("_retry")) if kind in ("tool", "llm", "agent") else (0, 1.0)
        attempts: list[dict[str, Any]] = []
        t_row = time.monotonic()
        token = _active_run_id.set(run_id)
        try:
            n = 0
            while True:
                n += 1
                t_a = time.monotonic()
                try:
                    if self.dry:
                        journal_result, outputs, fields = self._perform_dry(run_id, eff, kind, args)
                    else:
                        journal_result, outputs, fields = await self._perform(
                            run, seq, kind, key, eff.get("name"), args, options,
                        )
                except _Parked as p:
                    await self._emit_parked(run, seq, key, p.parked_on)
                    return True
                except _EffectFailure as e:
                    attempts.append({
                        "n": n, "error": f"{e.error_type}: {e}",
                        "ms": int((time.monotonic() - t_a) * 1000),
                    })
                    if isinstance(e, _RETRYABLE) and n <= extra:
                        await self._update_step_retry(step_run_id, n)
                        wait = min(backoff * (2 ** (n - 1)), _RETRY_BACKOFF_CAP)
                        log.info("playbook.v2.retry run_id=%s effect=%s attempt=%d backoff=%s", run_id, key, n, wait)
                        if wait > 0:
                            await asyncio.sleep(wait)
                        continue
                    await self.journal.fail(run_id, seq, e.error_type, str(e), attempts, extra=e.extra)
                    if self.dry:
                        # no step row, no step event (docs/v2.md §10)
                        return False
                    await self._complete_step(step_run_id, "failed", error=f"{e.error_type}: {e}", inputs=args)
                    await self._events.emit("playbook.step.failed", {
                        "run_id": run_id, "step_id": key, "error": f"{e.error_type}: {e}",
                        "retry_count": n - 1,
                    })
                    return False
                attempts.append({"n": n, "error": None, "ms": int((time.monotonic() - t_a) * 1000)})
                break
            ms = int((time.monotonic() - t_row) * 1000)
            await self.journal.complete(run_id, seq, journal_result, attempts, ms, extra=fields)
            if self.dry:
                return False
            await self._complete_step(step_run_id, "done", outputs=outputs, inputs=args)
            await self._events.emit("playbook.step.completed", {
                "run_id": run_id, "step_id": key, "outputs": outputs,
            })
            return False
        except asyncio.CancelledError:
            # cancel_run's task path (runner.cancel_run): the row that was in
            # flight fails RunCancelled; _drive_run marks the run cancelled.
            try:
                await self.journal.fail(run_id, seq, "RunCancelled", "run cancelled", attempts)
                if not self.dry:
                    await self._complete_step(step_run_id, "failed", error="run cancelled", inputs=args)
            except Exception:  # noqa: BLE001 — never mask the cancellation
                log.exception("playbook.v2.cancel_bookkeeping_failed run_id=%s", run_id)
            raise
        finally:
            _active_run_id.reset(token)

    def _perform_dry(
        self, run_id: str, eff: dict[str, Any], kind: str, args: dict[str, Any],
    ) -> tuple[Any, Any, dict[str, Any]]:
        """Dry mode (docs/v2.md §10): the answer comes from `stubs` or is a
        placeholder the jail rebuilds; `extra` stamps the row with
        `stubbed`/`stub_key`/`schema`/`effect`. Raises `_EffectFailure` only
        for the phase 07 `wait_event` timeout stub."""
        site_id, occurrence = self._split_key(eff, str(eff.get("id")))
        result, extra = dry_answer(
            str(kind), site_id, occurrence, self._stubs, args=args,
            rng=self._dry_rng.get(run_id),
        )
        if extra.pop("event_timeout", False):
            # phase 07: a `{"_event_timeout": true}` stub answers wait_event
            # with the timeout failure — the one dry answer that raises
            raise _EventTimeout(
                f"no '{args.get('name')}' event within {args.get('timeout')}s (dry stub)"
            )
        return result, None, extra

    async def _perform(
        self, run: Any, seq: int, kind: str, key: str, name: str | None, args: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[Any, Any, dict[str, Any] | None]:
        """Execute one effect attempt → (journal result, step-row outputs,
        extra journal fields or None)."""
        if kind == "tool":
            result, outputs = await self._perform_tool(run, key, str(name), args, options)
            return result, outputs, None
        if kind == "llm":
            return await self._effect_llm(run, key, args, options)
        if kind == "agent":
            return await self._effect_agent(run, key, args, options)
        if kind == "subtask":
            return await self._effect_subtask(run, key, str(name), args, options)
        if kind == "approve":
            return await self._effect_approve(run, seq, key, args, options)
        if kind == "wait_event":
            return await self._effect_wait_event(run, seq, key, args, options)
        if kind == "now":
            iso = _now().isoformat()
            return iso, {"now": iso}, None
        if kind == "random":
            value = SystemRandom().random()
            return value, {"random": value}, None
        if kind == "log":
            msg = args.get("message")
            out = {"message": msg if isinstance(msg, str) else str(msg)}
            log.info("playbook.v2.log run_id=%s %s", run.id, out["message"])
            return out, out, None
        raise _EffectFailure(f"effect kind {kind!r} is not available in this version")

    # ------------------------------------------------------------ llm / agent
    @staticmethod
    def _timeout_for(kind: str, options: dict[str, Any]) -> float | None:
        timeout = options.get("_timeout")
        if timeout is None:
            timeout = DEFAULT_TIMEOUTS.get(kind)
        return None if timeout is None else float(timeout)

    @staticmethod
    async def _bounded(coro: Any, timeout: float | None, key: str, label: str) -> Any:
        """`asyncio.wait_for` with the phase 02 `EffectTimeout` message; the
        inner task is cancelled on expiry."""
        if timeout is None:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout)
        except asyncio.TimeoutError:
            raise _EffectTimeout(
                f"effect '{key}' ({label}) timed out after {timeout:g}s"
            ) from None

    @staticmethod
    def _shape_answer(result: Any, output: Any) -> Any:
        """Return rule (docs/v2.md §2): `output=` given → dict (a non-dict
        answer is `{"_raw": result}`, v1's rule); else `str`."""
        if output is not None:
            return result if isinstance(result, dict) else {"_raw": result}
        if isinstance(result, str):
            return result
        if isinstance(result, (dict, list)):
            return json.dumps(result, default=str)
        return "" if result is None else str(result)

    @staticmethod
    def _cost_fields(usage: Any) -> dict[str, Any]:
        """`_record_step_cost`'s rule: cost only when `usage` carries a truthy
        `cost_cents`; the target is the journal row, not a step row."""
        cost = getattr(usage, "cost_cents", None) if usage else None
        return {"cost_cents": cost} if cost else {}

    def _require_agent(self, kind: str) -> Any:
        if self._agent is None:
            raise _EffectFailure(
                f"ctx.{kind} requires an injected agent (ctx.agent) but none was "
                "provided to the PlaybookRunner."
            )
        return self._agent

    async def _effect_llm(
        self, run: Any, key: str, args: dict[str, Any], options: dict[str, Any],
    ) -> tuple[Any, Any, dict[str, Any] | None]:
        agent = self._require_agent("llm")
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise _EffectFailure(f"effect '{key}': ctx.llm requires a prompt string")
        output = args.get("output")
        try:
            result, usage = await self._bounded(
                agent.run_llm(
                    prompt, purpose=args.get("purpose") or "summarization",
                    model=args.get("model"), system=args.get("system"),
                    output_schema=output,
                ),
                self._timeout_for("llm", options), key, "llm",
            )
        except (_EffectFailure, asyncio.CancelledError):
            raise
        except Exception as e:  # noqa: BLE001 — a raising facade fails the effect
            raise _EffectFailure(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__) from e
        value = self._shape_answer(result, output)
        return value, {"llm": {"prompt": prompt[:2000]}, "result": value}, self._cost_fields(usage) or None

    async def _effect_agent(
        self, run: Any, key: str, args: dict[str, Any], options: dict[str, Any],
    ) -> tuple[Any, Any, dict[str, Any] | None]:
        from ..delegation import _TranscriptFeed

        agent = self._require_agent("agent")
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise _EffectFailure(f"effect '{key}': ctx.agent requires a prompt string")
        output = args.get("output")
        feed = _TranscriptFeed()
        try:
            result, usage = await self._bounded(
                agent.run_turn(
                    prompt, output_schema=output, tools=args.get("tools"),
                    memory_write=False, conversation_id=getattr(run, "report_to", None),
                    event_stream_handler=feed.handle,
                ),
                self._timeout_for("agent", options), key, "agent",
            )
        except _EffectFailure as e:
            e.extra = {"transcript": list(feed.events), **(e.extra or {})}
            raise
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _EffectFailure(
                f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
                extra={"transcript": list(feed.events)},
            ) from e
        if isinstance(result, dict) and result.get("_aborted"):
            # the facade gave up (turn limit / timeout): fail loud, never a value
            raise _EffectFailure(
                str(result.get("error") or f"turn aborted ({result['_aborted']})"),
                extra={"transcript": list(feed.events)},
            )
        value = self._shape_answer(result, output)
        fields = {"transcript": list(feed.events), **self._cost_fields(usage)}
        return value, {"agent": {"prompt": prompt[:2000]}, "result": value}, fields

    # ------------------------------------------------------------ subtask
    async def _effect_subtask(
        self, run: Any, key: str, name: str, args: dict[str, Any], options: dict[str, Any],
    ) -> tuple[Any, Any, dict[str, Any] | None]:
        from sqlalchemy import select

        from ..models import Playbook
        from .checker import sniff_format

        if self._start_run is None:
            raise _EffectFailure(
                f"effect '{key}': ctx.subtask requires a run starter (start_run) but none "
                "was provided to the segment loop."
            )
        async with self._sf() as session:
            target = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
        if target is None:
            raise _EffectFailure(f"Subtask playbook '{name}' not found")
        if sniff_format(target.code) != "python":
            raise _EffectFailure(f"subtask target '{name}' is not a v2 playbook")
        state = self._runs.get(str(run.id))
        chain = list(state.chain) if state else []
        if name in chain:
            raise _EffectFailure(
                f"subtask '{name}' would recurse: it is already running in this "
                f"chain ({' -> '.join([*chain, name])}). A playbook cannot start "
                "itself or one of its ancestors."
            )
        inputs = args.get("inputs") if isinstance(args.get("inputs"), dict) else {}
        returns = args.get("returns")
        timeout = self._timeout_for("subtask", options)
        task = asyncio.create_task(
            self._start_run(
                target, inputs=dict(inputs), trigger=f"subtask:{run.id}",
                parent_run_id=run.id, is_test=bool(getattr(run, "is_test", False)),
            ),
            name=f"playbook-subtask-{run.id}-{key}",
        )
        timed_out = False
        child = None
        try:
            if timeout is None:
                child = await task
            else:
                done, _ = await asyncio.wait({task}, timeout=timeout)
                if not done:
                    # the host owns the deadline: `_drive_run` swallows the
                    # cancel and returns the row `cancelled`, so the flag —
                    # never the child's status — decides (Risks 12)
                    timed_out = True
                    task.cancel()
                child = await task
        except asyncio.CancelledError:
            # the parent was cancelled (or the cancelled child raised out of
            # `_create_run`): never leave the child task running
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    child = await task
            if not timed_out:
                raise
        extra = {"child_run_id": str(child.id)} if child is not None else None
        value = self._values.pop(str(child.id), None) if child is not None else None
        if timed_out:
            raise _EffectTimeout(
                f"effect '{key}' (subtask {name}) timed out after {timeout:g}s", extra=extra,
            )
        if child is None:
            raise _SubtaskFailed(f"subtask '{name}' did not produce a run", extra=extra)
        status = getattr(child, "status", None)
        if status != "done":
            detail = getattr(child, "error", None) or status or "unknown"
            raise _SubtaskFailed(
                f"subtask '{name}' (run {child.id}) {status}: {detail}", extra=extra,
            )
        if isinstance(returns, list):
            if not isinstance(value, dict):
                raise _SubtaskFailed(
                    f"subtask '{name}' (run {child.id}): returns={returns!r} needs a dict "
                    f"return value, got {type(value).__name__}", extra=extra,
                )
            missing = [k for k in returns if k not in value]
            if missing:
                raise _SubtaskFailed(
                    f"subtask '{name}' (run {child.id}): return value has no key(s) "
                    f"{missing}", extra=extra,
                )
            value = {k: value[k] for k in returns}
        return value, {"subtask": name, "run_id": str(child.id), "result": value}, extra

    # ------------------------------------------------------------ approve
    async def _effect_approve(
        self, run: Any, seq: int, key: str, args: dict[str, Any], options: dict[str, Any],
    ) -> tuple[Any, Any, dict[str, Any] | None]:
        approvals = getattr(self._ctx, "approval", None)
        if approvals is None:
            raise _EffectFailure(
                f"effect '{key}': ctx.approve requires an approval engine (ctx.approval) "
                "but none was provided."
            )
        state = self._runs.get(str(run.id))
        name = state.name if state else ""
        version = state.version if state else 0
        show = args.get("show")
        shown = show if isinstance(show, str) else json.dumps(show, indent=2, default=str)
        headline = f"Playbook '{name}' is asking for your approval"
        presentation = {
            "eyebrow": "Playbook approval",
            "headline": headline[:90],
            "explanation": (
                f"Run `{run.id}` of playbook `{name}` (version {version}) paused at "
                f"effect #{seq} (`{key}`) until you decide. Approving lets the run "
                "continue; rejecting raises `ctx.Rejected` inside the playbook."
            ),
            "changes": [{"label": "What the playbook shows", "kind": "text", "text": shown}],
        }
        summary = f"{headline}: {shown.splitlines()[0][:200] if shown else key}"
        timeout = options.get("_timeout")
        request_kw = {
            "kind": "playbook_effect",
            "summary": summary,
            "payload": {"run_id": str(run.id), "seq": int(seq), "playbook": name, "version": version},
            "requested_by_plugin": "plugin-playbooks",
            "risk_level": "medium",
            "conversation_id": getattr(run, "report_to", None),
            "presentation": presentation,
            "ttl_seconds": int(timeout) if timeout else None,
        }
        try:
            if self.park is not None:
                # phase 07 park form: `request_nowait`; `pending` parks the run
                decision, parked_on = await self.park.park_approval(run, seq, key, request_kw)
                if parked_on is not None:
                    raise _Parked(parked_on)
            else:
                decision = await approvals.request(**request_kw)
        except (asyncio.CancelledError, _Parked):
            raise
        except Exception as e:  # noqa: BLE001 — a failing engine fails the effect (closed)
            raise _EffectFailure(f"approval request failed: {type(e).__name__}: {e}") from e
        request_id = getattr(decision, "request_id", None)
        rid = str(request_id) if request_id is not None else None
        reason = getattr(decision, "reason", None)
        decided_by = getattr(decision, "decided_by", None)
        if getattr(decision, "decision", None) == "approved":
            result = {"approved": True, "request_id": rid, "reason": reason, "decided_by": decided_by}
            return result, {"approve": result}, None
        expired = await self._approval_expired(approvals, request_id, reason, decided_by)
        if expired:
            raise _ApprovalExpired(
                f"approval {rid or key} expired before a decision (ttl elapsed)"
            )
        raise _Rejected(
            f"approval {rid or key} rejected" + (f": {reason}" if reason else "")
        )

    async def _effect_wait_event(
        self, run: Any, seq: int, key: str, args: dict[str, Any], options: dict[str, Any],
    ) -> tuple[Any, Any, dict[str, Any] | None]:
        """Phase 07 `ctx.wait_event(name, filter, timeout=)`: always parks —
        the bus subscription lives in `ParkService`, not in this task."""
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise _EffectFailure(f"effect '{key}': ctx.wait_event needs an event name")
        timeout = args.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise _EffectFailure(
                f"effect '{key}': ctx.wait_event needs timeout= (seconds > 0), got {timeout!r}"
            )
        filt = args.get("filter")
        if filt is not None and not isinstance(filt, dict):
            raise _EffectFailure(f"effect '{key}': ctx.wait_event filter must be a dict")
        if self.park is None:
            raise _EffectFailure(
                f"effect '{key}': ctx.wait_event needs the park service — none on this runner"
            )
        parked_on = await self.park.park_event(run, seq, key, name, filt, float(timeout))
        raise _Parked(parked_on)

    async def _emit_parked(self, run: Any, seq: int, key: str, parked_on: dict[str, Any]) -> None:
        state = self._runs.get(str(run.id))
        log.info("playbook.v2.parked run_id=%s seq=%d effect=%s on=%s", run.id, seq, key, parked_on.get("kind"))
        if self._events is None:
            return
        await self._events.emit("playbook.run.parked", {
            "run_id": str(run.id),
            "playbook_id": str(getattr(run, "playbook_id", "") or ""),
            "playbook_name": state.name if state else "",
            "playbook_version": getattr(run, "playbook_version", None) or (state.version if state else None),
            "is_test": bool(getattr(run, "is_test", False)),
            "trigger": getattr(run, "trigger", None),
            "conversation_id": str(run.conversation_id) if getattr(run, "conversation_id", None) else None,
            "parent_run_id": str(run.parent_run_id) if getattr(run, "parent_run_id", None) else None,
            "wake_on_complete": bool(getattr(run, "wake_on_complete", False)),
            "seq": int(seq), "step_id": key, "parked_on": dict(parked_on),
        })

    @staticmethod
    async def _approval_expired(approvals: Any, request_id: Any, reason: Any, decided_by: Any) -> bool:
        """`approvals.get(request_id).status == "expired"` first; when `get` is
        absent (or knows no row) the TTL sweeper's stamp — reason "ttl elapsed"
        by "system" — is the fallback."""
        get = getattr(approvals, "get", None)
        if callable(get) and request_id is not None:
            try:
                req = get(request_id)
                if asyncio.iscoroutine(req):
                    req = await req
            except Exception:  # noqa: BLE001 — a failing lookup takes the fallback
                req = None
            status = getattr(req, "status", None) if req is not None else None
            if status is not None:
                return status == "expired"
        return reason == "ttl elapsed" and decided_by == "system"

    async def _perform_tool(
        self, run: Any, key: str, name: str, args: dict[str, Any], options: dict[str, Any],
    ) -> tuple[Any, Any]:
        from ..runner import _normalize_tool_result, resolve_vault_refs

        call_args = dict(args)
        # plans/016 phase 2 parity: a chat send that names no conversation
        # inherits the run's stamped report_to; a background live run has none.
        if name == "send_chat_message" and not call_args.get("conversation_id"):
            if getattr(run, "report_to", None) is not None:
                call_args["conversation_id"] = str(run.report_to)
            elif not getattr(run, "is_test", False):
                raise _ToolError(
                    f"effect '{key}': this run has no chat to report to — "
                    "scheduled/background runs do not deliver to the ops chat. "
                    "Give send_chat_message an explicit conversation_id."
                )
        try:
            rt = self._tools.get(name)
        except KeyError:
            raise _ToolError(
                f"effect '{key}': unknown tool '{name}' — it is not in the tool registry."
            ) from None
        try:
            call_args = await resolve_vault_refs(
                getattr(self._ctx, "vault", None), call_args, step_id=key,
            )
        except ValueError as e:
            raise _ToolError(str(e)) from None
        timeout = options.get("_timeout")
        if timeout is None:
            timeout = DEFAULT_TIMEOUTS.get("tool")
        try:
            if timeout is None:
                raw = await rt.handler(**call_args)
            else:
                raw = await asyncio.wait_for(rt.handler(**call_args), float(timeout))
        except asyncio.TimeoutError:
            raise _EffectTimeout(
                f"effect '{key}' ({name}) timed out after {timeout}s"
            ) from None
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a raising handler is a ToolError
            raise _ToolError(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__) from e
        normalized = _normalize_tool_result(raw)
        return normalized, {"tool": name, "result": normalized}

    # ------------------------------------------------------------ rows
    async def _create_step(self, run_id: Any, step_id: str, step_kind: str) -> Any:
        from ..models import PlaybookStepRun

        async with self._sf() as session:
            sr = PlaybookStepRun(
                run_id=run_id, step_id=step_id[:128], step_kind=str(step_kind)[:32],
                status="running", started_at=_now(),
            )
            session.add(sr)
            await session.commit()
            await session.refresh(sr)
            return sr.id

    async def _update_step_retry(self, step_run_id: Any, count: int) -> None:
        from ..models import PlaybookStepRun

        async with self._sf() as session:
            sr = await session.get(PlaybookStepRun, step_run_id)
            if sr:
                sr.retry_count = count
                await session.commit()

    async def _complete_step(
        self, step_run_id: Any, status: str, outputs: Any = None,
        error: str | None = None, inputs: Any = None,
    ) -> None:
        from ..models import PlaybookStepRun

        async with self._sf() as session:
            sr = await session.get(PlaybookStepRun, step_run_id)
            if sr:
                sr.status = status
                sr.outputs = outputs
                if inputs is not None:
                    sr.inputs = inputs
                sr.error = error
                sr.completed_at = _now()
                await session.commit()
