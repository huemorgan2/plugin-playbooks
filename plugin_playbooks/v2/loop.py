"""Host segment loop for v2 playbooks (plans/032 phase 02; docs/v2.md §6, §7, §11).

`SegmentLoop.drive(run, playbook, inputs)` runs a v2 playbook end to end on
segmented replay: it writes entry 0, then repeatedly invokes the jail
(`code_run` with `SHIM_SOURCE` and the envelope), dispatches on the shim's
`kind` — `effect`: journal `in_flight` FIRST, execute with v1 parity, complete
or fail the row, re-invoke; `gather`: the same for a batch (phase 03 emits
it); `return`: done; `error`: the run fails with the four run columns.

Runner internals (`_normalize_tool_result`, `_active_run_id`, vault
resolution, the step-row shapes) are imported lazily inside functions —
`runner.py` imports this package at module level.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from random import SystemRandom
from typing import Any

from . import DEFAULT_TIMEOUTS as _DEFAULT_TIMEOUTS
from . import MAX_EFFECTS
from .journal import JournalStore, make_effect_entry, make_entry0
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
    """Host-side effect failure with the journal `error.type`."""

    error_type = "EffectError"


class _ToolError(_EffectFailure):
    error_type = "ToolError"


class _EffectTimeout(_EffectFailure):
    error_type = "EffectTimeout"


_RETRYABLE = (_ToolError, _EffectTimeout)


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
        max_effects: int = MAX_EFFECTS,
    ) -> None:
        self._sf = session_factory
        self._tools = tools
        self._events = events
        self._ctx = ctx
        self.journal = journal
        self.segment_timeout = segment_timeout
        self.max_effects = max_effects
        self.last_result: LoopResult | None = None

    # ------------------------------------------------------------ drive
    async def drive(self, run: Any, playbook: Any, inputs: dict[str, Any]) -> LoopResult:
        from .checker import check

        run_id = str(run.id)
        name = playbook.name
        version = int(getattr(run, "playbook_version", None) or getattr(playbook, "live_version", 0) or 1)
        source = playbook.code or ""
        filename = f"playbook:{name}@v{version}"
        call_sites = check(source, name=name, version=version).summary.get("call_sites", [])
        hash_seed = secrets.randbelow(2**32)
        await self.journal.start(run_id, make_entry0(
            hash_seed=hash_seed, inputs=dict(inputs or {}), playbook=name, version=version,
            max_effects=self.max_effects,
        ))
        result = LoopResult()
        self.last_result = result
        try:
            rt = self._code_run_tool()
            while True:
                result.segments += 1
                n = result.segments
                journal = await self.journal.read(run_id)
                envelope = {
                    "playbook": name, "version": version, "source": source,
                    "hash_seed": hash_seed, "max_effects": self.max_effects,
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
                if kind == "return":
                    result.value = res.get("value")
                    return result
                if kind == "error":
                    raise self._run_error(res, source, filename)
                if kind == "effect":
                    await self._effect(run, res)
                    continue
                if kind == "gather":
                    await self._gather(run, res)
                    continue
                raise V2RunError(
                    f"segment {n}: unknown result kind {kind!r}", "ShimFailure",
                )
        finally:
            await self.journal.drop(run_id)

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
    async def _gather(self, run: Any, res: dict[str, Any]) -> None:
        """Batching hook (phase 03 emits `gather`): every effect is journaled
        `in_flight` in argument order, then executed concurrently; every row
        is completed/failed before the next segment."""
        effects = list(res.get("effects") or [])
        prepared = [await self._journal_and_start(run, eff) for eff in effects]
        await asyncio.gather(
            *(self._execute_and_finish(run, eff, seq, step_run_id) for eff, seq, step_run_id in prepared),
            return_exceptions=False,
        )

    async def _effect(self, run: Any, eff: dict[str, Any]) -> None:
        eff, seq, step_run_id = await self._journal_and_start(run, eff)
        await self._execute_and_finish(run, eff, seq, step_run_id)

    async def _journal_and_start(self, run: Any, eff: dict[str, Any]) -> tuple[dict[str, Any], int, Any]:
        run_id = str(run.id)
        kind = eff.get("effect_kind")
        key = str(eff.get("id"))
        site_id, occurrence = self._split_key(eff, key)
        args = eff.get("args") if isinstance(eff.get("args"), dict) else {}
        # journal FIRST (docs/v2.md §6) — the row exists before anything runs
        seq = await self.journal.append_in_flight(run_id, make_effect_entry(
            run_id=run_id, seq=int(eff.get("seq") or 0), kind=kind, id=site_id,
            occurrence=occurrence, name=eff.get("name"), args=args,
        ))
        if eff.get("seq") is not None and int(eff["seq"]) != seq:
            await self.journal.fail(run_id, seq, "JournalDivergence", "seq mismatch", [])
            raise V2RunError(
                f"the shim asked for effect seq {eff['seq']} but the journal is at {seq}: "
                "code edited under a run, or non-journaled randomness",
                "JournalDivergence",
            )
        step_run_id = await self._create_step(run.id, key, kind)
        await self._events.emit("playbook.step.started", {
            "run_id": run_id, "step_id": key, "step_kind": kind,
        })
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

    async def _execute_and_finish(self, run: Any, eff: dict[str, Any], seq: int, step_run_id: Any) -> None:
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
                    journal_result, outputs = await self._perform(
                        run, kind, key, eff.get("name"), args, options,
                    )
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
                    await self.journal.fail(run_id, seq, e.error_type, str(e), attempts)
                    await self._complete_step(step_run_id, "failed", error=f"{e.error_type}: {e}", inputs=args)
                    await self._events.emit("playbook.step.failed", {
                        "run_id": run_id, "step_id": key, "error": f"{e.error_type}: {e}",
                        "retry_count": n - 1,
                    })
                    return
                attempts.append({"n": n, "error": None, "ms": int((time.monotonic() - t_a) * 1000)})
                break
            ms = int((time.monotonic() - t_row) * 1000)
            await self.journal.complete(run_id, seq, journal_result, attempts, ms)
            await self._complete_step(step_run_id, "done", outputs=outputs, inputs=args)
            await self._events.emit("playbook.step.completed", {
                "run_id": run_id, "step_id": key, "outputs": outputs,
            })
        except asyncio.CancelledError:
            # cancel_run's task path (runner.cancel_run): the row that was in
            # flight fails RunCancelled; _drive_run marks the run cancelled.
            try:
                await self.journal.fail(run_id, seq, "RunCancelled", "run cancelled", attempts)
                await self._complete_step(step_run_id, "failed", error="run cancelled", inputs=args)
            except Exception:  # noqa: BLE001 — never mask the cancellation
                log.exception("playbook.v2.cancel_bookkeeping_failed run_id=%s", run_id)
            raise
        finally:
            _active_run_id.reset(token)

    async def _perform(
        self, run: Any, kind: str, key: str, name: str | None, args: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[Any, Any]:
        """Execute one effect attempt → (journal result, step-row outputs)."""
        if kind == "tool":
            return await self._perform_tool(run, key, str(name), args, options)
        if kind == "now":
            iso = _now().isoformat()
            return iso, {"now": iso}
        if kind == "random":
            value = SystemRandom().random()
            return value, {"random": value}
        if kind == "log":
            msg = args.get("message")
            out = {"message": msg if isinstance(msg, str) else str(msg)}
            log.info("playbook.v2.log run_id=%s %s", run.id, out["message"])
            return out, out
        raise _EffectFailure(f"effect kind {kind!r} is not wired until plugin/03")

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
