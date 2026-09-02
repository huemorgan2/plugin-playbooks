"""plans/020 phase 2 — the authed delegation API the dojoP bench grades
through: full terminal record (tool stream with args + ok, report) and a
newest-first listing."""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.models import Base, PlaybookDelegation


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    routes.init_routes(sf, runner=None)

    async with sf() as s:
        s.add(PlaybookDelegation(
            task="fix phones in candidate-intake — a long task description "
                 "that runs past one hundred and twenty characters so the "
                 "listing truncation is observable in this test",
            playbook="candidate-intake", status="done",
            card_token="tok-a",
            events=[{"ts": "t", "phase": "Change", "kind": "tool",
                     "label": "playbook_edit",
                     "args": {"name": "candidate-intake"},
                     "detail": "candidate_saved", "ok": True, "ms": 40}],
            result="PUBLISHED v4.", steps_used=1,
        ))
        s.add(PlaybookDelegation(
            task="newer job", playbook="", status="running",
            card_token="tok-b", steps_used=0,
        ))
        await s.commit()

    app = FastAPI()
    app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
    app.include_router(routes.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://luna.test"
    ) as client:
        yield client, sf
    await engine.dispose()


async def test_detail_returns_full_tool_stream(env):
    c, sf = env
    async with sf() as s:
        from sqlalchemy import select
        row = (await s.execute(
            select(PlaybookDelegation)
            .where(PlaybookDelegation.status == "done")
        )).scalar_one()
    r = await c.get(f"/api/p/plugin-playbooks/delegations/{row.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["result"] == "PUBLISHED v4."
    assert body["task"].startswith("fix phones")  # full task, not truncated
    ev = body["events"][0]
    assert ev["args"] == {"name": "candidate-intake"}
    assert ev["ok"] is True


async def test_list_is_newest_first_summaries(env):
    c, _sf = env
    r = await c.get("/api/p/plugin-playbooks/delegations?limit=10")
    assert r.status_code == 200
    rows = r.json()["delegations"]
    assert len(rows) == 2
    assert rows[0]["task"] == "newer job"
    assert len(rows[1]["task"]) <= 120  # summaries truncate the task
    assert "events" not in rows[0]  # stream only on the detail route
    assert "result" not in rows[0]


async def test_unknown_and_malformed_ids_404(env):
    c, _sf = env
    r = await c.get(f"/api/p/plugin-playbooks/delegations/{uuid.uuid4()}")
    assert r.status_code == 404
    r = await c.get("/api/p/plugin-playbooks/delegations/not-a-uuid")
    assert r.status_code == 404
