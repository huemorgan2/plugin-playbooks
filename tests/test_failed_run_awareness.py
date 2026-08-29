"""plans/014 — failed-run awareness.

The agent learns about failing playbooks through a prompt-section digest:
failed runs of the CURRENT live version, gated on a version-scoped ack.
These tests pin the scoping rules (they are the whole feature) against a
real database.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import _rel_age, failure_digest, render_failure_section
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookVersion


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Bus:
    async def emit(self, name, payload):  # noqa: ANN001
        pass


class _Runner:
    _tools = None
    _agent = None


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await engine.dispose()


async def _playbook(
    sf,
    *,
    name: str = "pb",
    version: int = 1,
    live_version: int = 0,
    status: str = "enabled",
    failures_acked_version: int | None = None,
) -> uuid.UUID:
    pid = uuid.uuid4()
    async with sf() as s:
        await s.execute(insert(Playbook).values(
            id=pid, name=name, definition={}, version=version,
            live_version=live_version, status=status,
            failures_acked_version=failures_acked_version,
        ))
        await s.commit()
    return pid


async def _run(
    sf, pid: uuid.UUID, *, version: int, status: str,
    started_at: datetime | None = None,
) -> uuid.UUID:
    rid = uuid.uuid4()
    async with sf() as s:
        await s.execute(insert(PlaybookRun).values(
            id=rid, playbook_id=pid, playbook_version=version, status=status,
            started_at=started_at or _now(),
        ))
        await s.commit()
    return rid


async def _digest(sf) -> list[dict]:
    async with sf() as s:
        return await failure_digest(s)


class TestDigestScoping:
    async def test_clean_playbook_yields_no_digest(self, db):
        pid = await _playbook(db, live_version=1)
        await _run(db, pid, version=1, status="done")
        assert await _digest(db) == []

    async def test_counts_only_live_version_failures(self, db):
        pid = await _playbook(db, version=12, live_version=12)
        for _ in range(3):  # old version — fixed by the last change
            await _run(db, pid, version=11, status="failed")
        await _run(db, pid, version=12, status="failed")
        await _run(db, pid, version=12, status="failed")
        await _run(db, pid, version=12, status="done")
        (d,) = await _digest(db)
        assert d["live_version"] == 12
        assert d["failed"] == 2
        assert d["finished"] == 3

    async def test_legacy_live_version_zero_falls_back_to_version(self, db):
        pid = await _playbook(db, version=4, live_version=0)
        await _run(db, pid, version=4, status="failed")
        (d,) = await _digest(db)
        assert d["live_version"] == 4
        assert d["failed"] == 1

    async def test_candidate_runs_are_excluded(self, db):
        pid = await _playbook(db, version=13, live_version=12)
        await _run(db, pid, version=13, status="failed")  # candidate test run
        assert await _digest(db) == []

    async def test_cancelled_and_running_are_not_failures(self, db):
        pid = await _playbook(db, live_version=1)
        await _run(db, pid, version=1, status="cancelled")
        await _run(db, pid, version=1, status="running")
        assert await _digest(db) == []
        await _run(db, pid, version=1, status="failed")
        (d,) = await _digest(db)
        assert d["failed"] == 1
        assert d["finished"] == 1  # cancelled/running don't count as finished

    async def test_disabled_playbook_is_excluded(self, db):
        pid = await _playbook(db, live_version=1, status="disabled")
        await _run(db, pid, version=1, status="failed")
        assert await _digest(db) == []

    async def test_detail_fields(self, db):
        pid = await _playbook(db, version=2, live_version=2)
        promoted = _now() - timedelta(days=3)
        async with db() as s:
            await s.execute(insert(PlaybookVersion).values(
                id=uuid.uuid4(), playbook_id=pid, version=2, definition={},
                created_at=promoted,
            ))
            await s.commit()
        await _run(db, pid, version=2, status="failed",
                   started_at=_now() - timedelta(hours=2))
        last = await _run(db, pid, version=2, status="failed",
                          started_at=_now() - timedelta(minutes=20))
        (d,) = await _digest(db)
        assert d["last_failed_run_id"] == str(last)
        assert d["promoted_at"] is not None


class TestAck:
    async def test_ack_of_live_version_silences(self, db):
        pid = await _playbook(db, version=12, live_version=12,
                              failures_acked_version=12)
        await _run(db, pid, version=12, status="failed")
        assert await _digest(db) == []

    async def test_promote_after_ack_rearms(self, db):
        # acked v12, then a new live version 13 fails — digest is back
        pid = await _playbook(db, version=13, live_version=13,
                              failures_acked_version=12)
        await _run(db, pid, version=13, status="failed")
        (d,) = await _digest(db)
        assert d["live_version"] == 13

    async def test_ack_tool_sets_live_version_and_clears_digest(self, db):
        pid = await _playbook(db, name="mailer", version=12, live_version=12)
        await _run(db, pid, version=12, status="failed")
        tools = {td.name: h for td, h in build_tools(db, _Bus(), _Runner())}
        assert (await _digest(db))[0]["name"] == "mailer"
        out = json.loads(await tools["playbook_ack_failures"](name="mailer"))
        assert out["status"] == "acked"
        assert out["acked_version"] == 12
        assert await _digest(db) == []

    async def test_ack_tool_unknown_playbook(self, db):
        tools = {td.name: h for td, h in build_tools(db, _Bus(), _Runner())}
        out = json.loads(await tools["playbook_ack_failures"](name="nope"))
        assert "error" in out

    @staticmethod
    async def _pid(sf, name: str) -> uuid.UUID:
        async with sf() as s:
            return (await s.execute(
                select(Playbook.id).where(Playbook.name == name)
            )).scalar_one()


class TestRendering:
    def test_empty_digest_renders_nothing(self):
        assert render_failure_section([]) == ""

    def test_renders_counts_ages_run_id_and_instructions(self):
        now = _now()
        text = render_failure_section([{
            "name": "daily-report",
            "live_version": 12,
            "failed": 47,
            "finished": 47,
            "last_failed_run_id": "7f3a0000-0000-0000-0000-000000000000",
            "last_failed_at": now - timedelta(minutes=20),
            "promoted_at": now - timedelta(days=3),
        }], now=now)
        assert "## Playbook failures needing your attention" in text
        assert "`daily-report`: 47 of 47 runs FAILED" in text
        assert "v12, promoted 3 days ago" in text
        assert "Last failure 20 minutes ago" in text
        assert "7f3a0000-0000-0000-0000-000000000000" in text
        assert "playbook_status" in text
        assert "playbook_ack_failures" in text
        assert "after finishing whatever they asked for" in text

    def test_missing_promoted_at_is_omitted(self):
        now = _now()
        text = render_failure_section([{
            "name": "pb", "live_version": 1, "failed": 1, "finished": 1,
            "last_failed_run_id": "x", "last_failed_at": now,
            "promoted_at": None,
        }], now=now)
        assert "(v1)" in text
        assert "promoted" not in text

    def test_rel_age_buckets_and_naive_datetimes(self):
        now = _now()
        assert _rel_age(now - timedelta(seconds=30), now) == "just now"
        assert _rel_age(now - timedelta(minutes=20), now) == "20 minutes ago"
        assert _rel_age(now - timedelta(hours=5), now) == "5 hours ago"
        assert _rel_age(now - timedelta(days=3), now) == "3 days ago"
        naive = (now - timedelta(hours=2)).replace(tzinfo=None)
        assert _rel_age(naive, now) == "2 hours ago"
        assert _rel_age(None, now) == "at an unknown time"


class TestPromptSection:
    async def test_prompt_sections_appends_digest_section(self, db):
        from plugin_playbooks import PlaybooksPlugin

        pid = await _playbook(db, name="mailer", live_version=1)
        await _run(db, pid, version=1, status="failed")
        plugin = PlaybooksPlugin()
        plugin._session_factory = db
        sections = await plugin.prompt_sections()
        assert len(sections) == 2
        assert "Your playbooks" in sections[0]
        assert "failures needing your attention" in sections[1]

    async def test_prompt_sections_single_section_when_clean(self, db):
        pid = await _playbook(db, name="mailer", live_version=1)
        await _run(db, pid, version=1, status="done")
        from plugin_playbooks import PlaybooksPlugin

        plugin = PlaybooksPlugin()
        plugin._session_factory = db
        sections = await plugin.prompt_sections()
        assert len(sections) == 1


class _KindStateCtx:
    def __init__(self, kind=None, state=None):
        self._kind, self._state = kind, state

    def conversation_kind(self):
        return self._kind

    def conversation_state(self):
        return self._state


class TestPromptSectionPerState:
    """089 §5: kind/state come from the ctx accessors (no-arg signature)."""

    async def _plugin(self, db, kind, state):
        from plugin_playbooks import PlaybooksPlugin

        plugin = PlaybooksPlugin()
        plugin._session_factory = db
        plugin._ctx = _KindStateCtx(kind, state)
        return plugin

    async def test_planning_drops_must_use_rule(self, db):
        await _playbook(db, name="mailer", live_version=1)
        plugin = await self._plugin(db, "building", "planning")
        text = "\n".join(await plugin.prompt_sections())
        assert "`mailer`" in text
        assert "MUST" not in text
        assert "Prefer running" not in text

    async def test_building_softens_rule(self, db):
        await _playbook(db, name="mailer", live_version=1)
        plugin = await self._plugin(db, "building", "building")
        text = "\n".join(await plugin.prompt_sections())
        assert "Prefer running an existing playbook" in text
        assert "you MUST use it" not in text

    async def test_ops_renders_mode_section_and_digest(self, db):
        pid = await _playbook(db, name="mailer", live_version=1)
        await _run(db, pid, version=1, status="failed")
        plugin = await self._plugin(db, "ops", "identify")
        sections = await plugin.prompt_sections()
        assert "Ops chat — mode: Identify" in sections[0]
        assert any("failures needing your attention" in s for s in sections)

    async def test_ops_fix_publish_mentions_gate(self, db):
        await _playbook(db, name="mailer", live_version=1)
        plugin = await self._plugin(db, "ops", "fix_publish")
        sections = await plugin.prompt_sections()
        assert "Fix & publish" in sections[0]
        assert "playbook_run_candidate" in sections[0]

    async def test_building_chat_omits_digest(self, db):
        pid = await _playbook(db, name="mailer", live_version=1)
        await _run(db, pid, version=1, status="failed")
        plugin = await self._plugin(db, "building", "building")
        sections = await plugin.prompt_sections()
        assert not any("failures needing your attention" in s for s in sections)
