"""plans/035-fix18fails P1.5 — the wake moment for a FAILED run carries what
the agent needs to act (error type, traceback, failing step + inputs) and tells
it to continue the original request; the send() result is inspected so an
aborted wake turn is logged as aborted, not delivered.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import wake as wake_mod
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookStepRun
from plugin_playbooks.wake import RunCompletionWake, _fmt_failure_detail


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await engine.dispose()


class _Send:
    def __init__(self, result=None):
        self.calls: list[dict] = []
        self.result = result if result is not None else {"responded": True}

    async def __call__(self, title, content, **kw):
        self.calls.append({"title": title, "content": content, **kw})
        return self.result


async def _failed_run(sf, *, with_step: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    now = datetime.now(timezone.utc)
    pid, rid = uuid.uuid4(), uuid.uuid4()
    async with sf() as s:
        await s.execute(insert(Playbook).values(
            id=pid, name="daily-digest", definition={}, version=1, live_version=1,
            status="enabled",
        ))
        await s.execute(insert(PlaybookRun).values(
            id=rid, playbook_id=pid, playbook_version=1, trigger="agent",
            status="failed", started_at=now, completed_at=now,
            error="line 12: http.get → HTTPError: 502 after effect e3",
            error_type="HTTPError",
            traceback="Traceback (playbook frames):\n  line 12, in fetch\n    r = http.get(url)\nHTTPError: 502 Bad Gateway",
            failed_at=now, wake_on_complete=True,
        ))
        if with_step:
            await s.execute(insert(PlaybookStepRun).values(
                id=uuid.uuid4(), run_id=rid, step_id="fetch", step_kind="http",
                status="done", inputs={"url": "https://x.test/ok"}, started_at=now,
            ))
            await s.execute(insert(PlaybookStepRun).values(
                id=uuid.uuid4(), run_id=rid, step_id="summarise", step_kind="agent_step",
                status="failed", inputs={"prompt": "summarise", "source": "https://x.test/feed"},
                error="HTTPError: 502 Bad Gateway", started_at=now,
            ))
        await s.commit()
    return pid, rid


def _payload(pid, rid, **over):
    base = {
        "run_id": str(rid), "playbook_id": str(pid), "playbook_name": "daily-digest",
        "status": "failed", "trigger": "agent", "conversation_id": str(uuid.uuid4()),
        "wake_on_complete": True, "duration_ms": 4200,
        "error": "line 12: http.get → HTTPError: 502 after effect e3",
    }
    base.update(over)
    return base


async def test_wake_moment_carries_failure_detail_and_continue_sentence(db):
    pid, rid = await _failed_run(db)
    send = _Send()
    svc = RunCompletionWake(db, events=None, ctx=None)
    await svc._wake_moment(send, _payload(pid, rid))

    assert len(send.calls) == 1
    body = send.calls[0]["content"]
    assert "status 'failed'" in body
    assert "Error: line 12: http.get" in body
    assert "Error type: HTTPError" in body
    assert "Failing step: summarise (agent_step)" in body
    assert "Step error: HTTPError: 502" in body
    assert '"source": "https://x.test/feed"' in body
    assert "Traceback:" in body and "HTTPError: 502 Bad Gateway" in body
    assert "Then continue what the original request asked for" in body
    assert "report honestly" in body
    # containment caps: the cumulative meter must not cut a real working turn
    assert send.calls[0]["token_budget"] == 1_500_000
    assert send.calls[0]["max_turns"] == 20


async def test_watch_moment_carries_the_same_detail(db):
    pid, rid = await _failed_run(db)
    send = _Send()
    svc = RunCompletionWake(db, events=None, ctx=None)
    await svc._watch_moment(send, _payload(pid, rid), uuid.uuid4(), "tell me when it lands")
    body = send.calls[0]["content"]
    assert "Error type: HTTPError" in body
    assert "Failing step: summarise" in body
    assert "Then continue what the original request asked for" in body
    assert "Your note when you set the watch: tell me when it lands" in body


async def test_success_moment_has_no_failure_block(db):
    pid, rid = await _failed_run(db, with_step=False)
    send = _Send()
    svc = RunCompletionWake(db, events=None, ctx=None)
    await svc._wake_moment(send, _payload(pid, rid, status="done", error=None))
    body = send.calls[0]["content"]
    assert "Error type" not in body and "Traceback" not in body
    assert "Report the outcome to the owner now" in body


def test_traceback_is_capped_at_the_tail():
    tb = "\n".join(f"frame {i}" for i in range(400))
    lines = _fmt_failure_detail(
        error_type="X", traceback=tb, step_id=None, step_kind=None, step_error=None, step_inputs=None,
    )
    joined = "\n".join(lines)
    assert "frame 399" in joined and "frame 0\n" not in joined
    assert "(earlier frames cut)" in joined
    assert len(joined) < wake_mod._TRACEBACK_CAP + 200


async def test_aborted_send_result_is_logged_as_aborted(db, caplog):
    pid, rid = await _failed_run(db)
    send = _Send(result={"responded": False, "aborted": "token_budget"})
    svc = RunCompletionWake(db, events=None, ctx=None)
    with caplog.at_level(logging.INFO, logger=wake_mod.log.name):
        await svc._wake_moment(send, _payload(pid, rid))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("run_wake.moment_aborted" in m and "token_budget" in m for m in msgs), msgs
    assert not any(m.startswith("run_wake.moment ") for m in msgs), "an abort is not a delivery"


async def test_error_send_result_is_logged_as_failed(db, caplog):
    pid, rid = await _failed_run(db)
    send = _Send(result={"responded": False, "error": "turn failed: RuntimeError: boom"})
    svc = RunCompletionWake(db, events=None, ctx=None)
    with caplog.at_level(logging.INFO, logger=wake_mod.log.name):
        await svc._wake_moment(send, _payload(pid, rid))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("run_wake.moment_failed" in m and "boom" in m for m in msgs), msgs
