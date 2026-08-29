"""PlaybookTriggerService — subscribes to bus events and starts playbook runs.

On startup, scans enabled playbooks for triggers and registers bus subscriptions.
When an event matches a trigger's filter, starts a playbook run with mapped inputs.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from jinja2 import Undefined
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from luna_sdk import EventBus

from .definition import TriggerDef
from .models import Playbook

log = logging.getLogger("luna.playbooks.triggers")

_SANDBOX_ENV = SandboxedEnvironment(undefined=Undefined)


class PlaybookTriggerService:
    """Watches the event bus and fires playbook runs on matching triggers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        events: EventBus,
        runner: Any,
    ) -> None:
        self._sf = session_factory
        self._events = events
        self._runner = runner
        # event name -> unsubscribe callable (EventBus.subscribe return value)
        self._unsubs: dict[str, Any] = {}
        # 0.26.0 (plans/015, 089 §6): single-flight — key of every trigger
        # delivery whose run is still alive. A concurrent IDENTICAL delivery
        # (same playbook, event, mapped inputs) is dropped instead of
        # starting a duplicate run; the key clears when the run's task ends.
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        """Scan all enabled playbooks and subscribe to their trigger events."""
        async with self._sf() as session:
            playbooks = (await session.execute(
                select(Playbook).where(Playbook.status == "enabled")
            )).scalars().all()

        event_map: dict[str, list[tuple[Playbook, TriggerDef]]] = {}
        for pb in playbooks:
            definition = pb.definition or {}
            for trigger_data in definition.get("triggers", []):
                try:
                    trigger = TriggerDef.model_validate(trigger_data)
                except Exception:
                    log.warning("trigger.invalid playbook=%s trigger=%s", pb.name, trigger_data)
                    continue
                if trigger.event:
                    event_map.setdefault(trigger.event, []).append((pb, trigger))

        for event_name, entries in event_map.items():
            captured_entries = list(entries)

            async def _handler(payload: Any, _entries=captured_entries) -> None:
                for pb, trigger in _entries:
                    if _matches_filter(payload, trigger.filter):
                        if trigger.if_expr and not _eval_if(trigger.if_expr, payload):
                            continue
                        mapped = _apply_map(trigger.map, payload)
                        flight_key = self._flight_key(pb, trigger.event, mapped)
                        if flight_key in self._in_flight:
                            # 089 §6 single-flight: identical concurrent
                            # delivery — one run is already going.
                            log.info(
                                "trigger.deduped playbook=%s bus_event=%s",
                                pb.name, trigger.event,
                            )
                            continue
                        log.info(
                            "trigger.fired playbook=%s bus_event=%s",
                            pb.name, trigger.event,
                        )
                        try:
                            # plans/009: background start — a slow playbook
                            # must not block the event bus handler for its
                            # whole duration. Execution errors are handled and
                            # logged inside the runner's drive loop.
                            self._in_flight.add(flight_key)
                            run = await self._runner.start_run_background(
                                pb, inputs=mapped, trigger=trigger.event,
                            )
                            task = getattr(self._runner, "_tasks", {}).get(run.id)
                            if task is not None:
                                task.add_done_callback(
                                    lambda _t, k=flight_key:
                                        self._in_flight.discard(k)
                                )
                            else:
                                # run already finished (or untracked) —
                                # nothing left to guard.
                                self._in_flight.discard(flight_key)
                        except Exception:
                            self._in_flight.discard(flight_key)
                            log.exception("trigger.run_failed playbook=%s", pb.name)

            # NB: EventBus.subscribe (there is no .on — the old call made
            # start() crash silently and no trigger ever fired).
            # 0.26.0 (plans/015, 089 §6): background=True — the handler must
            # run off the emitting task so a bus event fired inside a chat
            # turn cannot start a playbook run within that turn's context.
            # Older cores don't take the flag; fall back to inline dispatch
            # (report_to stamping in the runner still keeps delivery clean).
            try:
                self._unsubs[event_name] = self._events.subscribe(
                    event_name, _handler, background=True,
                )
            except TypeError:
                self._unsubs[event_name] = self._events.subscribe(
                    event_name, _handler,
                )
            log.info("trigger.subscribed bus_event=%s count=%s", event_name, len(entries))

    @staticmethod
    def _flight_key(pb: Playbook, event: str, inputs: dict[str, Any]) -> str:
        try:
            canon = json.dumps(inputs, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            canon = repr(inputs)
        return f"{pb.id}|{event}|{canon}"

    async def stop(self) -> None:
        """Clean up subscriptions."""
        for unsub in self._unsubs.values():
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass
        self._unsubs.clear()

    async def resync(self) -> None:
        """Re-read enabled playbooks and rebuild subscriptions.

        Called when a playbook is saved/enabled/disabled so new event
        triggers go live without a server restart."""
        await self.stop()
        await self.start()


def _matches_filter(payload: Any, filter_dict: dict[str, Any]) -> bool:
    """Flat equality match on payload keys with dot-path support."""
    if not filter_dict:
        return True
    if not isinstance(payload, dict):
        return False
    for key, expected in filter_dict.items():
        actual = _dot_get(payload, key)
        if actual != expected:
            return False
    return True


def _dot_get(d: dict, path: str) -> Any:
    """Get a value from a nested dict using dot-path notation."""
    parts = path.split(".")
    current: Any = d
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _eval_if(expr: str, payload: Any) -> bool:
    """Evaluate a sandboxed Jinja boolean expression."""
    try:
        tpl = _SANDBOX_ENV.from_string("{{ " + expr + " }}")
        result = tpl.render(event={"payload": payload})
        return result.strip().lower() in ("true", "1", "yes")
    except Exception:
        return False


def _apply_map(map_dict: dict[str, str], payload: Any) -> dict[str, Any]:
    """Apply Jinja templates from the map dict against the event payload."""
    if not map_dict:
        return {"payload": payload}
    result = {}
    for key, template in map_dict.items():
        try:
            tpl = _SANDBOX_ENV.from_string(template)
            rendered = tpl.render(event={"payload": payload})
            result[key] = rendered
        except Exception:
            result[key] = None
    return result
