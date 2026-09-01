"""plans/016 phase 2 — the ops chat carries exceptions only.

The runner's send_chat_message gate: a run's stamped report chat is
authoritative for tool steps. Chat-started runs inherit their chat; explicit
conversation_id always wins; a background live run has NO chat and the step
fails LOUD instead of leaning on core's ops fallback.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.models import Base, Playbook, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner

ORIGIN = uuid.uuid4()
OPS = uuid.uuid4()


class _Bus:
    async def emit(self, name, payload):
        pass


class _Tool:
    def __init__(self, handler) -> None:
        self.handler = handler


class _Tools:
    def __init__(self, **tools) -> None:
        self._tools = tools

    def get(self, name):
        return self._tools[name]

    def names(self):
        return list(self._tools)


class _Ctx:
    def __init__(self, origin=None, ops=OPS) -> None:
        self.current_conversation_id = origin
        self._ops = ops

    async def ops_conversation_id(self):
        return self._ops


def _greeter(args: dict) -> Playbook:
    return Playbook(
        name="greeter", display_name="greeter", status="enabled",
        version=1, live_version=1,
        definition={"name": "greeter", "steps": [
            {"id": "say", "kind": "tool_call", "tool": "send_chat_message",
             "args": args},
        ]},
    )


async def _env(ctx, args):
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    sent: list[dict] = []

    async def send(**kw):
        sent.append(kw)
        return {"ok": True}

    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(send_chat_message=_Tool(send)),
        events=_Bus(),
        context=ctx,
    )
    pb = _greeter(args)
    async with sf() as s:
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
    return engine, sf, runner, pb, sent


@pytest.mark.asyncio
async def test_chat_run_injects_origin_conversation():
    engine, _, runner, pb, sent = await _env(
        _Ctx(origin=ORIGIN), {"message": "hi"},
    )
    try:
        run = await runner.start_run(pb, trigger="agent")
        assert run.status == "done"
        assert sent[0]["conversation_id"] == str(ORIGIN)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_explicit_conversation_id_wins():
    explicit = str(uuid.uuid4())
    engine, _, runner, pb, sent = await _env(
        _Ctx(origin=ORIGIN),
        {"message": "hi", "conversation_id": explicit},
    )
    try:
        run = await runner.start_run(pb, trigger="agent")
        assert run.status == "done"
        assert sent[0]["conversation_id"] == explicit
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_background_live_run_refuses_implicit_send():
    # a leaked origin contextvar must not matter: bus/cron triggers stamp no
    # report chat, and the implicit send fails loud.
    engine, sf, runner, pb, sent = await _env(
        _Ctx(origin=ORIGIN), {"message": "hi"},
    )
    try:
        run = await runner.start_run(pb, trigger="cron:daily")
        assert run.status == "failed"
        assert sent == []
        async with sf() as s:
            step = (await s.execute(
                select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
            )).scalar_one()
        assert step.status == "failed"
        assert "no chat to report to" in step.error
        assert "explicit" in step.error and "conversation_id" in step.error
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_background_run_with_explicit_conversation_still_delivers():
    explicit = str(uuid.uuid4())
    engine, _, runner, pb, sent = await _env(
        _Ctx(), {"message": "hi", "conversation_id": explicit},
    )
    try:
        run = await runner.start_run(pb, trigger="cron:daily")
        assert run.status == "done"
        assert sent[0]["conversation_id"] == explicit
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_headless_test_run_reports_to_ops():
    # test runs keep their pre-016 delivery: origin chat, else ops.
    engine, _, runner, pb, sent = await _env(_Ctx(), {"message": "hi"})
    try:
        run = await runner.start_run(pb, is_test=True)
        assert run.status == "done"
        assert sent[0]["conversation_id"] == str(OPS)
    finally:
        await engine.dispose()
