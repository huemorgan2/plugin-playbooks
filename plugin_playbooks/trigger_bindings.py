"""TriggerBindingService — acquires/releases external triggers via the SDK trigger registry.

The consumer half of 006.713. When an enabled playbook declares an event
trigger whose event matches a TriggerInfo advertised in the registry, this
service calls ensure_trigger() on the owning source so the provider-side
instance goes live. When no enabled playbook needs it anymore, it calls
release_trigger(). Zero imports from publisher plugins.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from luna_sdk import TriggerSourceRegistry

from .models import Playbook

log = logging.getLogger("luna.playbooks.bindings")


class TriggerBindingService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: TriggerSourceRegistry,
    ) -> None:
        self._sf = session_factory
        self._registry = registry
        # trigger slug -> source_name, for slugs this service has ensured
        self._held: dict[str, str] = {}

    async def sync(self) -> dict[str, Any]:
        """Reconcile held triggers with what enabled playbooks need.

        Transitions only: newly-needed slugs get ensure_trigger once,
        no-longer-needed slugs get release_trigger once. Idempotent.
        """
        needed_events = await self._needed_events()
        available = await self._registry.all_triggers()

        desired: dict[str, str] = {}  # slug -> source_name
        for info in available:
            if info.event_pattern in needed_events:
                desired[info.slug] = info.source

        acquired, released, errors = [], [], []

        for slug, source_name in desired.items():
            if slug in self._held:
                continue
            source = self._registry.source(source_name)
            if source is None:
                continue
            try:
                instance_id = await source.ensure_trigger(slug, {})
                self._held[slug] = source_name
                acquired.append(slug)
                log.info("bindings.acquired trigger=%s instance=%s", slug, instance_id)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{slug}: {e}")
                log.warning("bindings.ensure_failed trigger=%s error=%s", slug, e)

        for slug in list(self._held):
            if slug in desired:
                continue
            source = self._registry.source(self._held[slug])
            self._held.pop(slug, None)
            if source is None:
                continue
            try:
                await source.release_trigger(slug)
                released.append(slug)
                log.info("bindings.released trigger=%s", slug)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{slug}: {e}")
                log.warning("bindings.release_failed trigger=%s error=%s", slug, e)

        return {"acquired": acquired, "released": released, "errors": errors}

    async def _needed_events(self) -> set[str]:
        """Event names referenced by triggers of enabled playbooks."""
        async with self._sf() as session:
            playbooks = (await session.execute(
                select(Playbook).where(Playbook.status == "enabled")
            )).scalars().all()
        events: set[str] = set()
        for pb in playbooks:
            for trigger in (pb.definition or {}).get("triggers", []):
                event = trigger.get("event")
                if event:
                    events.add(event)
        return events
