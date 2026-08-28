"""plans/013 phase 2 — the unauthed capability-token card route.

The srcdoc card iframe is opaque-origin: no cookie, no bearer header. Access
control is the per-delegation random token — so the route must 404 alike for
unknown ids and wrong tokens (no oracle), stay read-only, serve CORS-open
JSON, and prefer the live in-process feed over the throttled DB flush.
"""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.delegation import _LIVE_FEEDS, _EventFeed
from plugin_playbooks.models import Base, PlaybookDelegation

TOKEN = "sekrit-token-abc"


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    routes.init_routes(sf, runner=None)

    row = PlaybookDelegation(
        task="fix phones", playbook="candidate-intake",
        status="running", card_token=TOKEN,
        events=[{"ts": "t", "phase": "Change", "kind": "tool",
                 "label": "playbook_edit", "detail": "", "ms": 40}],
        steps_used=1,
    )
    async with sf() as s:
        s.add(row)
        await s.commit()
        await s.refresh(row)

    app = FastAPI()
    app.include_router(routes.ui_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://luna.test"
    ) as client:
        yield client, sf, row
    _LIVE_FEEDS.clear()
    await engine.dispose()


def _url(did, token):
    return f"/api/p/plugin-playbooks/delegations/{did}/card?token={token}"


async def test_wrong_or_missing_token_and_unknown_id_all_404(env):
    c, _sf, row = env
    assert (await c.get(_url(row.id, "wrong"))).status_code == 404
    assert (
        await c.get(f"/api/p/plugin-playbooks/delegations/{row.id}/card")
    ).status_code == 404
    assert (await c.get(_url(uuid.uuid4(), TOKEN))).status_code == 404
    assert (await c.get(_url("not-a-uuid", TOKEN))).status_code == 404


async def test_right_token_returns_running_shape_with_cors(env):
    c, _sf, row = env
    r = await c.get(_url(row.id, TOKEN))
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["status"] == "running"
    assert body["playbook"] == "candidate-intake"
    assert body["steps_used"] == 1
    assert body["result"] is None  # never leak a partial result mid-run
    assert body["events"][0]["label"] == "playbook_edit"
    assert body["started_at"]


async def test_live_feed_beats_stale_db_row(env):
    c, sf, row = env
    feed = _EventFeed(sf, row.id)
    feed._append("tool", "playbook_spec_run", phase="Prove")
    feed._append("tool", "playbook_promote", phase="Ship")
    feed.steps_used = 3
    _LIVE_FEEDS[row.id] = feed  # newer than the flushed row

    body = (await c.get(_url(row.id, TOKEN))).json()
    assert body["steps_used"] == 3
    assert [e["label"] for e in body["events"]] == [
        "playbook_spec_run", "playbook_promote",
    ]


async def test_terminal_shape_carries_result_and_ignores_live_feed(env):
    c, sf, row = env
    async with sf() as s:
        fresh = await s.get(PlaybookDelegation, row.id)
        fresh.status = "done"
        fresh.result = "Promoted v4; 8/8 specs green."
        await s.commit()

    _LIVE_FEEDS[row.id] = _EventFeed(sf, row.id)  # leftover feed must not win
    body = (await c.get(_url(row.id, TOKEN))).json()
    assert body["status"] == "done"
    assert "Promoted v4" in body["result"]
    assert body["events"][0]["label"] == "playbook_edit"  # from the row


async def test_events_tail_capped(env):
    c, sf, row = env
    feed = _EventFeed(sf, row.id)
    for i in range(450):
        feed._append("tool", f"t{i}", phase="Prove")
    _LIVE_FEEDS[row.id] = feed
    body = (await c.get(_url(row.id, TOKEN))).json()
    assert len(body["events"]) == 200
    assert body["events"][-1]["label"] == "t449"
