"""plans/001 — run stats in the playbook list, against a real database.

The point of these tests is the cost: the list must read run history in a
bounded number of grouped queries, no matter how many playbooks or runs exist.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import routes
from plugin_playbooks.models import Base, Playbook, PlaybookRun

def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Row:
    """What ``_run_stats`` needs off a playbook: its id and when it was made."""

    def __init__(self, pid: uuid.UUID, created_at: datetime) -> None:
        self.id = pid
        self.created_at = created_at


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    counter = {"selects": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, many):  # noqa: ANN001
        if statement.lstrip().upper().startswith("SELECT"):
            counter["selects"] += 1

    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf, counter
    await engine.dispose()


async def _make(sf, *, playbooks: list[tuple[uuid.UUID, datetime]], runs: list[tuple[uuid.UUID, datetime]]):
    async with sf() as s:
        for pid, created in playbooks:
            await s.execute(insert(Playbook).values(
                id=pid, name=f"pb-{pid.hex[:8]}", definition={}, created_at=created,
                updated_at=created,
            ))
        for pid, started in runs:
            await s.execute(insert(PlaybookRun).values(
                id=uuid.uuid4(), playbook_id=pid, playbook_version=1,
                status="succeeded", started_at=started,
            ))
        await s.commit()


@pytest.fixture(autouse=True)
def _clean_cache():
    routes._reset_stats_cache()
    yield
    routes._reset_stats_cache()


class TestRunStats:
    async def test_last_run_and_rate_come_back_per_playbook(self, db):
        sf, _ = db
        now = _now()
        a, b = uuid.uuid4(), uuid.uuid4()
        created = now - timedelta(days=60)
        await _make(
            sf,
            playbooks=[(a, created), (b, created)],
            runs=[(a, now - timedelta(hours=2)), (a, now - timedelta(days=3)),
                  (b, now - timedelta(days=1))],
        )
        async with sf() as s:
            stats = await routes._run_stats(s, [_Row(a, created), _Row(b, created)])

        assert stats[str(a)]["runs_window"] == 2
        assert stats[str(b)]["runs_window"] == 1
        # 2 runs over a full 30-day window.
        assert stats[str(a)]["runs_per_day"] == pytest.approx(0.1)
        assert datetime.fromisoformat(stats[str(a)]["last_run_at"]) > \
            datetime.fromisoformat(stats[str(b)]["last_run_at"])

    async def test_only_the_window_counts_but_the_last_run_still_shows(self, db):
        sf, _ = db
        now = _now()
        pid = uuid.uuid4()
        created = now - timedelta(days=400)
        await _make(sf, playbooks=[(pid, created)],
                    runs=[(pid, now - timedelta(days=200))])
        async with sf() as s:
            stats = await routes._run_stats(s, [_Row(pid, created)])

        st = stats[str(pid)]
        assert st["runs_window"] == 0
        assert st["runs_per_day"] == 0.0
        # A playbook that ran 200 days ago is not "never run".
        assert st["last_run_at"] is not None

    async def test_never_run_reads_as_nothing(self, db):
        sf, _ = db
        now = _now()
        pid = uuid.uuid4()
        await _make(sf, playbooks=[(pid, now)], runs=[])
        async with sf() as s:
            stats = await routes._run_stats(s, [_Row(pid, now)])
        assert stats[str(pid)] == {
            "last_run_at": None, "runs_window": 0, "runs_per_day": 0.0,
        }

    async def test_a_young_playbook_is_rated_over_the_days_it_existed(self):
        now = _now()
        # Created yesterday, ran 4 times: 4/day, not 4/30.
        assert routes._runs_per_day(4, now - timedelta(days=1), now) == 4.0
        # Younger than a day never divides by a fraction.
        assert routes._runs_per_day(3, now - timedelta(hours=2), now) == 3.0
        # Older than the window is rated over the window.
        assert routes._runs_per_day(30, now - timedelta(days=365), now) == 1.0
        # No created_at (legacy row) falls back to the full window.
        assert routes._runs_per_day(30, None, now) == 1.0

    async def test_many_playbooks_cost_a_fixed_number_of_queries(self, db):
        sf, counter = db
        now = _now()
        created = now - timedelta(days=60)
        actives = [uuid.uuid4() for _ in range(25)]
        idles = [uuid.uuid4() for _ in range(25)]
        await _make(
            sf,
            playbooks=[(p, created) for p in actives + idles],
            runs=[(p, now - timedelta(hours=i + 1)) for i, p in enumerate(actives)]
                 + [(p, now - timedelta(days=99)) for p in idles],
        )
        rows = [_Row(p, created) for p in actives + idles]
        async with sf() as s:
            counter["selects"] = 0
            stats = await routes._run_stats(s, rows)

        # One grouped query for the window, one bounded query for the idle ids.
        assert counter["selects"] == 2
        assert len(stats) == 50
        assert all(stats[str(p)]["runs_window"] == 1 for p in actives)
        assert all(stats[str(p)]["last_run_at"] is not None for p in idles)

    async def test_all_recent_means_a_single_query(self, db):
        sf, counter = db
        now = _now()
        created = now - timedelta(days=60)
        ids = [uuid.uuid4() for _ in range(5)]
        await _make(sf, playbooks=[(p, created) for p in ids],
                    runs=[(p, now - timedelta(days=1)) for p in ids])
        async with sf() as s:
            counter["selects"] = 0
            await routes._run_stats(s, [_Row(p, created) for p in ids])
        assert counter["selects"] == 1

    async def test_a_second_load_inside_the_ttl_touches_the_database_once(self, db):
        sf, counter = db
        now = _now()
        created = now - timedelta(days=60)
        ids = [uuid.uuid4() for _ in range(3)]
        await _make(sf, playbooks=[(p, created) for p in ids],
                    runs=[(p, now - timedelta(days=1)) for p in ids])
        rows = [_Row(p, created) for p in ids]
        async with sf() as s:
            counter["selects"] = 0
            first = await routes._run_stats(s, rows)
            second = await routes._run_stats(s, rows)
            third = await routes._run_stats(s, rows)

        assert counter["selects"] == 1
        assert first == second == third

    async def test_the_cache_expires(self, db, monkeypatch):
        sf, counter = db
        now = _now()
        created = now - timedelta(days=60)
        pid = uuid.uuid4()
        await _make(sf, playbooks=[(pid, created)],
                    runs=[(pid, now - timedelta(days=1))])
        rows = [_Row(pid, created)]
        async with sf() as s:
            counter["selects"] = 0
            await routes._run_stats(s, rows)
            monkeypatch.setattr(routes, "_STATS_TTL_SECONDS", -1.0)
            await routes._run_stats(s, rows)
        assert counter["selects"] == 2

    async def test_starting_a_run_makes_the_next_load_recompute(self, db):
        sf, counter = db
        now = _now()
        created = now - timedelta(days=60)
        pid = uuid.uuid4()
        await _make(sf, playbooks=[(pid, created)], runs=[])
        rows = [_Row(pid, created)]
        async with sf() as s:
            await routes._run_stats(s, rows)
            await _make(sf, playbooks=[], runs=[(pid, _now())])
            routes._reset_stats_cache()
            after = await routes._run_stats(s, rows)
        assert after[str(pid)]["runs_window"] == 1


class TestIndex:
    def test_the_run_table_is_indexed_for_this_read(self):
        names = {i.name for i in PlaybookRun.__table__.indexes}
        assert "ix_playbook_runs_playbook_started" in names
        idx = next(i for i in PlaybookRun.__table__.indexes
                   if i.name == "ix_playbook_runs_playbook_started")
        assert [c.name for c in idx.columns] == ["playbook_id", "started_at"]
