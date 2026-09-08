"""plans/032 phase 07 — parked runs (docs/v2.md §2 `ctx.approve` park form,
`ctx.wait_event`; §6 `parked` / `parked_on`; §11 "Parked").

`ParkService` owns everything that happens to a run while no task drives it:

- `park_approval` raises the owner card with `approvals.request_nowait` and,
  when the answer is `pending`, writes the park (journal entry `parked` +
  run row `parked`/`parked_on`) and arms the deadline timer. An inline
  decision (grant hit) is handed back to the loop unchanged. Cores without
  `request_nowait` fall back to phase 03's in-process `request()` form.
- `park_event` subscribes the run to the bus (`events.subscribe`) and parks it
  the same way; the handler resumes on the first payload matching `filter`.
- `on_approval_decided` is the `approval.decided` handler; timers detect card
  expiry (which emits nothing) and event timeouts; `reconcile()` rebuilds
  subscriptions, timers and the approval index after a restart.
- `_resume` is the ONE path every resume goes through: it re-checks
  `status == "parked"`, flips the row to `running`, completes or fails the
  parking journal entry and its step row, then spawns `runner.resume(run)`
  (phase 06's `_resume_run` → `SegmentLoop.resume`).
- `release` (cancel / max_duration) rejects the card or drops the
  subscription; `_fail_loudly` fails the run through `runner._complete_run`
  so `playbook.run.completed` fires like any failure.

`due_at` is the earlier of the effect deadline (card TTL, wait timeout) and
`started_at + max_duration` (`MAX_DURATION_S`, 24 h by default).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import types
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

from . import MAX_DURATION_S

log = logging.getLogger("luna.plugins.playbooks.v2.park")

PARK_KINDS = frozenset({"approve", "wait_event"})

# Card expiry has no bus signal: the timer polls `approvals.get()` and, when
# the engine still says `pending` (no sweeper — the in-memory engine), releases
# the card itself after `EXPIRY_POLLS` × `EXPIRY_POLL_S` (Risks 3).
EXPIRY_POLL_S = 5.0
EXPIRY_POLLS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return _utc(dt).isoformat() if dt is not None else None


def _parse(s: Any) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return _utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def _uuid(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return value


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
        return await value
    return value


def _decision_object(request_id: Any, *, reason: str, decided_by: str = "system") -> Any:
    """A rejected decision for `approvals.decide()` — a namespace carrying
    every attribute the core's `decide` reads (`decision`, `reason`,
    `edited_payload`, `decided_by`, `decided_at`, `lifetime`, `expires_at`;
    luna `approval/db_impl.py` `decide` reads attributes only). The plugin
    never imports core (`tests/test_manifest.py::test_no_core_imports`), so
    the real `ApprovalDecision` class is not used (Risks 7)."""
    return types.SimpleNamespace(
        request_id=_uuid(request_id), decision="rejected", reason=reason,
        edited_payload=None, decided_by=decided_by, decided_at=_now(),
        lifetime="once", expires_at=None,
    )


class ParkService:
    def __init__(
        self, session_factory: Any, events: Any, ctx: Any, journal: Any, resume: Callable[..., Any],
        *, max_duration: float = MAX_DURATION_S, complete_run: Callable[..., Any] | None = None,
    ) -> None:
        self._sf = session_factory
        self._events = events
        self._ctx = ctx
        self._journal = journal
        # `runner.resume(run)` — spawns phase 06's `_resume_run` for one run
        self._spawn = resume
        # `runner._complete_run(run_id, status, error=, error_type=, failed_at=)`
        self._complete_run = complete_run
        self.max_duration = float(max_duration)
        self.expiry_poll_s = EXPIRY_POLL_S
        self.expiry_polls = EXPIRY_POLLS
        self._index: dict[str, str] = {}          # approval_id -> run_id
        self._timers: dict[str, asyncio.Task] = {}  # run_id -> deadline timer
        self._unsubs: dict[str, Callable[[], Any]] = {}  # run_id -> bus unsubscribe
        # approval ids this service decided itself (release / self-expiry):
        # their `approval.decided` echo must not resume anything
        self._muted: set[str] = set()
        self._unsub_decided: Callable[[], Any] | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Subscribe `approval.decided` (idempotent). Loop-independent, so
        `on_load` may call it — the handler runs in the emitter's task."""
        if self._unsub_decided is not None:
            return
        subscribe = getattr(self._events, "subscribe", None)
        if not callable(subscribe):
            log.warning("playbook.v2.park: event bus has no subscribe() — parked approvals resume on reconcile only")
            return
        self._unsub_decided = subscribe("approval.decided", self.on_approval_decided)

    def stop(self) -> None:
        if self._unsub_decided is not None:
            with contextlib.suppress(Exception):
                self._unsub_decided()
            self._unsub_decided = None
        for run_id in list(self._timers):
            self._cancel_timer(run_id)
        for run_id in list(self._unsubs):
            self._drop_subscription(run_id)
        self._index.clear()

    @property
    def subscribed(self) -> bool:
        return self._unsub_decided is not None

    # ------------------------------------------------------------ parking
    async def park_approval(
        self, run: Any, seq: int, effect_id: str, request_kw: dict[str, Any],
    ) -> tuple[Any, dict[str, Any] | None]:
        """Raise the card. → (decision, None) when the engine answered inline
        (or has no `request_nowait`: phase 03's blocking form), else
        (None, parked_on) after the park is written and armed."""
        approvals = getattr(self._ctx, "approval", None)
        nowait = getattr(approvals, "request_nowait", None) if approvals is not None else None
        if not callable(nowait):
            log.info("playbook.v2.park: approval engine has no request_nowait — in-process approve (run %s)", run.id)
            return await approvals.request(**request_kw), None
        decision = await nowait(**request_kw)
        if getattr(decision, "decision", None) != "pending":
            return decision, None
        approval_id = str(getattr(decision, "request_id", None))
        since = _now()
        ttl = request_kw.get("ttl_seconds")
        effect_deadline = since + timedelta(seconds=float(ttl)) if ttl else None
        parked_on = {
            "kind": "approval", "approval_id": approval_id,
            "since": _iso(since), "due_at": _iso(self._due(run, effect_deadline)),
        }
        await self._write_park(run, seq, parked_on)
        self._index[approval_id] = str(run.id)
        self._arm(str(run.id), parked_on["due_at"])
        log.info("playbook.v2.park.approval run_id=%s effect=%s approval=%s due=%s", run.id, effect_id, approval_id, parked_on["due_at"])
        return None, parked_on

    async def park_event(
        self, run: Any, seq: int, effect_id: str, name: str, filter: dict[str, Any] | None, timeout: float,
    ) -> dict[str, Any]:
        since = _now()
        effect_deadline = since + timedelta(seconds=float(timeout))
        parked_on = {
            "kind": "event", "event_name": name,
            "since": _iso(since), "due_at": _iso(self._due(run, effect_deadline)),
        }
        await self._write_park(run, seq, parked_on)
        self._subscribe(str(run.id), name, filter)
        self._arm(str(run.id), parked_on["due_at"])
        log.info("playbook.v2.park.event run_id=%s effect=%s event=%s due=%s", run.id, effect_id, name, parked_on["due_at"])
        return parked_on

    def _due(self, run: Any, effect_deadline: datetime | None) -> datetime:
        run_deadline = self._run_deadline(run)
        if effect_deadline is None or run_deadline <= effect_deadline:
            return run_deadline
        return effect_deadline

    def _run_deadline(self, run: Any) -> datetime:
        started = _utc(getattr(run, "started_at", None)) or _now()
        return started + timedelta(seconds=self.max_duration)

    async def _write_park(self, run: Any, seq: int, parked_on: dict[str, Any]) -> None:
        """Journal first (the entry goes `parked`, so `in_flight()` never
        returns it), then the run row."""
        from ..models import PlaybookRun

        await self._journal.park(str(run.id), int(seq), parked_on)
        async with self._sf() as session:
            row = await session.get(PlaybookRun, run.id)
            if row is not None:
                row.status = "parked"
                row.parked_on = dict(parked_on)
                await session.commit()

    # ------------------------------------------------------------ subscriptions / timers
    def _subscribe(self, run_id: str, name: str, filter: dict[str, Any] | None) -> None:
        self._drop_subscription(run_id)
        wanted = dict(filter or {})

        async def handler(payload: Any, *_a: Any, **_kw: Any) -> None:
            if not isinstance(payload, dict):
                return
            if any(payload.get(k) != v for k, v in wanted.items()):
                return
            await self._resume(run_id, done=dict(payload))

        self._unsubs[run_id] = self._events.subscribe(name, handler)

    def _drop_subscription(self, run_id: str) -> None:
        unsub = self._unsubs.pop(run_id, None)
        if unsub is not None:
            with contextlib.suppress(Exception):
                unsub()

    def subscriptions(self) -> int:
        return len(self._unsubs)

    def _arm(self, run_id: str, due_at: str | None) -> None:
        self._cancel_timer(run_id)
        due = _parse(due_at)
        if due is None:
            return
        delay = max(0.0, (due - _now()).total_seconds())
        self._timers[run_id] = asyncio.create_task(
            self._timer(run_id, delay), name=f"playbook-park-{run_id}",
        )

    def _cancel_timer(self, run_id: str) -> None:
        task = self._timers.pop(run_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _timer(self, run_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.on_due(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a timer must never die silently
            log.exception("playbook.v2.park.timer_failed run_id=%s", run_id)
        finally:
            if self._timers.get(run_id) is asyncio.current_task():
                self._timers.pop(run_id, None)

    async def on_due(self, run_id: str) -> None:
        """The deadline fired (or a test fires it by hand): re-check the row,
        then max_duration → fail loudly; event → EventTimeout; approval →
        poll the card for expiry."""
        row = await self._load(run_id)
        if row is None or row.status != "parked" or not isinstance(row.parked_on, dict):
            return
        parked_on = row.parked_on
        kind = parked_on.get("kind")
        since = parked_on.get("since")
        what = (
            f"approval #{parked_on.get('approval_id')}" if kind == "approval"
            else f"event '{parked_on.get('event_name')}'"
        )
        if _now() >= self._run_deadline(row) - timedelta(milliseconds=1):
            await self._fail_loudly(
                run_id, "MaxDurationExceeded",
                f"parked on {what} since {since} — run max_duration ({self.max_duration:g}s) exceeded",
            )
            return
        if kind == "event":
            timeout = self._seconds_between(since, parked_on.get("due_at"))
            await self._resume(run_id, failed=(
                "EventTimeout", f"no '{parked_on.get('event_name')}' event within {timeout}s",
            ))
            return
        if kind == "approval":
            await self._poll_expiry(run_id, str(parked_on.get("approval_id")))

    @staticmethod
    def _seconds_between(since: Any, due_at: Any) -> Any:
        a, b = _parse(since), _parse(due_at)
        if a is None or b is None:
            return "?"
        secs = (b - a).total_seconds()
        return int(secs) if float(secs).is_integer() else round(secs, 3)

    async def _poll_expiry(self, run_id: str, approval_id: str) -> None:
        approvals = getattr(self._ctx, "approval", None)
        for n in range(self.expiry_polls + 1):
            status = await self._card_status(approvals, approval_id)
            if status == "expired":
                await self._resume(run_id, failed=("ApprovalExpired", "ttl elapsed"))
                return
            if status in ("approved", "rejected"):
                await self._resume_from_status(run_id, approval_id, status)
                return
            if status is None:
                await self._fail_loudly(run_id, "ApprovalExpired", f"approval card {approval_id} no longer exists")
                return
            if n < self.expiry_polls:
                await asyncio.sleep(self.expiry_poll_s)
        # the engine never swept it (no sweeper): release the card ourselves
        await self._decide(approvals, approval_id, reason="ttl elapsed")
        await self._resume(run_id, failed=("ApprovalExpired", "ttl elapsed"))

    async def _card_status(self, approvals: Any, approval_id: str) -> str | None:
        get = getattr(approvals, "get", None) if approvals is not None else None
        if not callable(get):
            return "pending"
        try:
            req = await _maybe_await(get(_uuid(approval_id)))
        except Exception:  # noqa: BLE001 — a failing lookup counts as gone
            log.exception("playbook.v2.park.get_failed approval=%s", approval_id)
            return None
        if req is None:
            return None
        return getattr(req, "status", None) or "pending"

    async def _decide(self, approvals: Any, approval_id: str, *, reason: str) -> None:
        decide = getattr(approvals, "decide", None) if approvals is not None else None
        if not callable(decide):
            return
        self._muted.add(approval_id)
        try:
            await _maybe_await(decide(_uuid(approval_id), _decision_object(approval_id, reason=reason)))
        except (KeyError, ValueError) as e:
            # gone (purged after expiry) / already decided — both fine
            log.info("playbook.v2.park.release_noop approval=%s: %s", approval_id, e)
        except Exception:  # noqa: BLE001
            log.exception("playbook.v2.park.release_failed approval=%s", approval_id)

    # ------------------------------------------------------------ decisions
    async def on_approval_decided(self, payload: Any, *_a: Any, **_kw: Any) -> None:
        if not isinstance(payload, dict):
            return
        approval_id = str(payload.get("id"))
        if approval_id in self._muted:
            self._muted.discard(approval_id)
            return
        run_id = self._index.get(approval_id)
        if run_id is None:
            run_id = await self._find_parked_on_approval(approval_id)
        if run_id is None:
            return
        decision = payload.get("decision")
        if decision == "approved":
            await self._resume(run_id, done={
                "approved": True, "request_id": approval_id,
                "reason": payload.get("reason"), "decided_by": payload.get("decided_by"),
            })
        elif decision == "rejected":
            await self._resume(run_id, failed=("Rejected", payload.get("reason") or "rejected"))

    async def _resume_from_status(self, run_id: str, approval_id: str, status: str) -> None:
        """A decision learned through `get()` (restart, expiry poll): the
        request carries `status` only — `reason`/`decided_by` are None (Risks 18)."""
        if status == "approved":
            await self._resume(run_id, done={
                "approved": True, "request_id": approval_id, "reason": None, "decided_by": None,
            })
        elif status == "rejected":
            await self._resume(run_id, failed=("Rejected", "rejected"))
        elif status == "expired":
            await self._resume(run_id, failed=("ApprovalExpired", "ttl elapsed"))

    async def _find_parked_on_approval(self, approval_id: str) -> str | None:
        for row in await self._parked_rows():
            po = row.parked_on if isinstance(row.parked_on, dict) else {}
            if po.get("kind") == "approval" and str(po.get("approval_id")) == approval_id:
                self._index[approval_id] = str(row.id)
                return str(row.id)
        return None

    # ------------------------------------------------------------ the one resume path
    async def _resume(
        self, run_id: str, *, done: Any = None, failed: tuple[str, str] | None = None,
    ) -> bool:
        """Every resume path ends here. Returns False when the row is no
        longer `parked` (cancelled, failed by max_duration, already resumed)."""
        from ..models import PlaybookRun

        rid = _uuid(run_id)
        async with self._sf() as session:
            row = await session.get(PlaybookRun, rid)
            if row is None or row.status != "parked":
                self._forget(run_id)
                return False
            parked_on = dict(row.parked_on or {})
            row.status = "running"
            row.parked_on = None
            await session.commit()
        self._forget(run_id, parked_on)
        entry = await self._parked_entry(run_id)
        if entry is not None:
            seq = int(entry["seq"])
            key = f"{entry.get('id')}#{entry.get('occurrence')}"
            ms = self._elapsed_ms(entry.get("started_at"))
            if failed is None:
                await self._journal.complete(run_id, seq, done, [{"n": 1, "error": None, "ms": ms}], ms)
                await self._close_step(run_id, key, "done", outputs=self._outputs(entry, done))
                await self._events.emit("playbook.step.completed", {
                    "run_id": run_id, "step_id": key, "outputs": self._outputs(entry, done),
                })
            else:
                error_type, message = failed
                await self._journal.fail(run_id, seq, error_type, message, [
                    {"n": 1, "error": f"{error_type}: {message}", "ms": ms},
                ])
                await self._close_step(run_id, key, "failed", error=f"{error_type}: {message}")
                await self._events.emit("playbook.step.failed", {
                    "run_id": run_id, "step_id": key, "error": f"{error_type}: {message}",
                    "retry_count": 0,
                })
        else:
            log.warning("playbook.v2.park.no_parked_entry run_id=%s", run_id)
        run = await self._load(run_id)
        if run is None:
            return False
        log.info("playbook.v2.park.resume run_id=%s outcome=%s", run_id, "done" if failed is None else failed[0])
        self._spawn(run)
        return True

    @staticmethod
    def _outputs(entry: dict[str, Any], done: Any) -> Any:
        if entry.get("kind") == "approve":
            return {"approve": done}
        return {"event": done}

    @staticmethod
    def _elapsed_ms(started_at: Any) -> int:
        started = _parse(started_at)
        return int((_now() - started).total_seconds() * 1000) if started else 0

    def _forget(self, run_id: str, parked_on: dict[str, Any] | None = None) -> None:
        self._cancel_timer(run_id)
        self._drop_subscription(run_id)
        for approval_id, rid in list(self._index.items()):
            if rid == run_id:
                self._index.pop(approval_id, None)
        if parked_on and parked_on.get("kind") == "approval":
            self._index.pop(str(parked_on.get("approval_id")), None)

    # ------------------------------------------------------------ release / fail
    async def release(self, run_id: Any, *, reason: str | None = None, error_type: str = "RunCancelled",
                      message: str = "run cancelled") -> dict[str, Any] | None:
        """Cancel / max_duration: reject the card (no withdraw on the protocol)
        or drop the subscription, cancel the timer, fail the parking journal
        entry and its step row. Returns the `parked_on` that was released;
        the caller completes the run row. The row's own status is left to
        the caller, but `parked_on` is cleared so a late `approval.decided`
        cannot find it."""
        from ..models import PlaybookRun

        run_id = str(run_id)
        async with self._sf() as session:
            row = await session.get(PlaybookRun, _uuid(run_id))
            if row is None or row.status != "parked":
                return None
            parked_on = dict(row.parked_on or {})
            row.parked_on = None
            await session.commit()
        self._forget(run_id, parked_on)
        if parked_on.get("kind") == "approval":
            await self._decide(
                getattr(self._ctx, "approval", None), str(parked_on.get("approval_id")),
                reason=reason or f"playbook run {run_id} cancelled",
            )
        entry = await self._parked_entry(run_id)
        if entry is not None:
            seq = int(entry["seq"])
            key = f"{entry.get('id')}#{entry.get('occurrence')}"
            await self._journal.fail(run_id, seq, error_type, message, [
                {"n": 1, "error": f"{error_type}: {message}", "ms": self._elapsed_ms(entry.get("started_at"))},
            ])
            await self._close_step(run_id, key, "failed", error=f"{error_type}: {message}")
            await self._events.emit("playbook.step.failed", {
                "run_id": run_id, "step_id": key, "error": f"{error_type}: {message}", "retry_count": 0,
            })
        log.info("playbook.v2.park.released run_id=%s kind=%s", run_id, parked_on.get("kind"))
        return parked_on

    async def _fail_loudly(self, run_id: str, error_type: str, message: str) -> None:
        released = await self.release(
            run_id, reason=f"playbook run {run_id} exceeded max_duration" if error_type == "MaxDurationExceeded"
            else f"playbook run {run_id} failed: {message}",
            error_type=error_type, message=message,
        )
        if released is None:
            return
        log.warning("playbook.v2.park.failed run_id=%s type=%s: %s", run_id, error_type, message)
        if self._complete_run is not None:
            await self._complete_run(
                _uuid(run_id), "failed", error=message, error_type=error_type, failed_at=_now(),
            )

    # ------------------------------------------------------------ reconcile
    async def reconcile(self) -> int:
        """`on_server_ready`: rebuild the state of every `parked` row —
        subscriptions, timers, the approval index — or resume at once when
        the deadline passed / the card was decided while the server was down."""
        approvals = getattr(self._ctx, "approval", None)
        n = 0
        for row in await self._parked_rows():
            run_id = str(row.id)
            po = row.parked_on if isinstance(row.parked_on, dict) else {}
            kind = po.get("kind")
            due = _parse(po.get("due_at"))
            n += 1
            if kind == "event":
                if due is not None and due <= _now():
                    await self.on_due(run_id)
                    continue
                entry = await self._parked_entry(run_id)
                args = (entry or {}).get("args") if entry else None
                filt = args.get("filter") if isinstance(args, dict) else None
                self._subscribe(run_id, str(po.get("event_name")), filt if isinstance(filt, dict) else None)
                self._arm(run_id, po.get("due_at"))
                continue
            if kind == "approval":
                approval_id = str(po.get("approval_id"))
                status = await self._card_status(approvals, approval_id)
                if status is None:
                    await self._fail_loudly(run_id, "ApprovalExpired", f"approval card {approval_id} no longer exists")
                elif status == "pending":
                    self._index[approval_id] = run_id
                    if due is not None and due <= _now():
                        await self.on_due(run_id)
                    else:
                        self._arm(run_id, po.get("due_at"))
                else:
                    await self._resume_from_status(run_id, approval_id, status)
                continue
            log.warning("playbook.v2.park.reconcile_unknown_kind run_id=%s parked_on=%r", run_id, po)
        if n:
            log.info("playbook.v2.park.reconciled count=%d", n)
        return n

    # ------------------------------------------------------------ rows
    async def _load(self, run_id: str) -> Any:
        from ..models import PlaybookRun

        async with self._sf() as session:
            return await session.get(PlaybookRun, _uuid(run_id))

    async def _parked_rows(self) -> list[Any]:
        from ..models import PlaybookRun

        async with self._sf() as session:
            return list((await session.execute(
                select(PlaybookRun).where(PlaybookRun.status == "parked")
            )).scalars().all())

    async def _parked_entry(self, run_id: str) -> dict[str, Any] | None:
        for entry in await self._journal.read(run_id):
            if entry.get("seq", 0) and entry.get("status") == "parked":
                return entry
        return None

    async def _close_step(self, run_id: str, step_id: str, status: str, *, outputs: Any = None,
                          error: str | None = None) -> None:
        from ..models import PlaybookStepRun

        async with self._sf() as session:
            rows = (await session.execute(
                select(PlaybookStepRun).where(
                    PlaybookStepRun.run_id == _uuid(run_id), PlaybookStepRun.step_id == step_id[:128],
                    PlaybookStepRun.status == "running",
                )
            )).scalars().all()
            for sr in rows:
                sr.status = status
                sr.outputs = outputs
                sr.error = error
                sr.completed_at = _now()
            if rows:
                await session.commit()
