"""0.31.0 (plans/019) — playbooks as an ops provider.

With plugin-ops installed (provider key "ops"), live-run failures are
reported on the event bus instead of raising fix-proposal cards, mutation
tools honor the approved plan's scope, and gated publishes report outcomes.
Without it, everything degrades to the 0.30.x behavior byte-for-byte.
"""

from __future__ import annotations

import inspect
import json
import uuid

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.fix_proposals import FixProposalService, failure_signature
from plugin_playbooks.models import (
    Base,
    Playbook,
    PlaybookFixProposal,
    PlaybookRun,
    PlaybookStepRun,
)
from plugin_playbooks.ops_provider import (
    ops_authority,
    report_outcome,
    report_problem,
    scope_refusal,
)


# ---------------------------------------------------------------- fakes


class _Bus:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, name, payload):  # noqa: ANN001
        self.emitted.append((name, payload))


class _BrokenBus:
    async def emit(self, name, payload):  # noqa: ANN001
        raise RuntimeError("bus down")


class _Authority:
    def __init__(self, plan=None, raises=False):  # noqa: ANN001
        self._plan = plan
        self._raises = raises

    async def active_plan(self):
        if self._raises:
            raise RuntimeError("db down")
        return self._plan


class _Registry:
    def __init__(self, ops=None):  # noqa: ANN001
        self._ops = ops

    def has(self, key: str) -> bool:
        return key == "ops" and self._ops is not None

    def get(self, key: str):
        if not self.has(key):
            raise KeyError(key)
        return self._ops


class _Ctx:
    def __init__(self, *, ops=None, kind=None, registry=True):  # noqa: ANN001
        if registry:
            self.provider_registry = _Registry(ops)
        self._kind = kind

    def conversation_kind(self):
        return self._kind


# ---------------------------------------------------------------- authority


def test_no_registry_means_no_ops():
    assert ops_authority(_Ctx(registry=False)) is None
    assert ops_authority(None) is None


def test_registry_without_ops_key():
    assert ops_authority(_Ctx(ops=None)) is None


def test_registry_with_ops():
    auth = _Authority()
    assert ops_authority(_Ctx(ops=auth)) is auth


# ---------------------------------------------------------------- problem


@pytest.mark.asyncio
async def test_report_problem_emits_when_ops_present():
    bus = _Bus()
    ok = await report_problem(
        _Ctx(ops=_Authority()), bus,
        name="pb", signature="s" * 40, display_name="Nice name",
        purpose="does things", run_id="r1", version=3, step_id="step-2",
        error="E" * 500,
    )
    assert ok is True
    ((event, payload),) = bus.emitted
    assert event == "ops.problem_reported"
    assert payload["provider"] == "plugin-playbooks"
    assert payload["area_ref"] == "playbook:pb"
    assert payload["signature"] == "s" * 40
    assert payload["evidence"]["run_id"] == "r1"
    assert payload["evidence"]["step"] == "step-2"
    assert len(payload["evidence"]["error"]) == 400  # truncated
    assert payload["display"] == {"name": "Nice name", "purpose": "does things"}


@pytest.mark.asyncio
async def test_report_problem_false_without_ops():
    bus = _Bus()
    ok = await report_problem(
        _Ctx(ops=None), bus,
        name="pb", signature="s", display_name="n", purpose="",
        run_id="r", version=1, step_id="", error="",
    )
    assert ok is False
    assert bus.emitted == []


@pytest.mark.asyncio
async def test_report_problem_false_when_emit_raises():
    # a broken bus must fall back to the card path, not lose the failure
    ok = await report_problem(
        _Ctx(ops=_Authority()), _BrokenBus(),
        name="pb", signature="s", display_name="n", purpose="",
        run_id="r", version=1, step_id="", error="boom",
    )
    assert ok is False


# ---------------------------------------------------------------- scope


def _plan(scope: str, targets: list[str]):
    return {"plan_id": "p-1", "scope": scope, "targets": targets,
            "area_ref": targets[0] if targets else None, "status": "executing"}


@pytest.mark.asyncio
async def test_scope_non_ops_conversation_never_gates():
    ctx = _Ctx(ops=_Authority(_plan("plan_only", [])), kind="building")
    assert await scope_refusal(ctx, "pb") is None


@pytest.mark.asyncio
async def test_scope_ops_chat_without_plugin_ops():
    assert await scope_refusal(_Ctx(ops=None, kind="ops"), "pb") is None


@pytest.mark.asyncio
async def test_scope_no_active_plan():
    ctx = _Ctx(ops=_Authority(None), kind="ops")
    assert await scope_refusal(ctx, "pb") is None


@pytest.mark.asyncio
async def test_scope_anything_needed_never_gates():
    ctx = _Ctx(ops=_Authority(_plan("anything_needed", [])), kind="ops")
    assert await scope_refusal(ctx, "pb") is None


@pytest.mark.asyncio
async def test_scope_plan_only_declared_target_passes():
    plan = _plan("plan_only", ["playbook:pb", "playbook:other"])
    ctx = _Ctx(ops=_Authority(plan), kind="ops")
    assert await scope_refusal(ctx, "pb") is None


@pytest.mark.asyncio
async def test_scope_plan_only_undeclared_target_refused():
    plan = _plan("plan_only", ["playbook:other"])
    ctx = _Ctx(ops=_Authority(plan), kind="ops")
    out = json.loads(await scope_refusal(ctx, "pb"))
    assert out["gate"] == "ops_plan_scope"
    assert out["plan_id"] == "p-1"
    assert "playbook:pb" in out["error"]
    assert "ops_file_plan" in out["hint"]  # steering, not a dead end


@pytest.mark.asyncio
async def test_scope_broken_active_plan_query_fails_open():
    # a broken query must not brick every mutation tool
    ctx = _Ctx(ops=_Authority(raises=True), kind="ops")
    assert await scope_refusal(ctx, "pb") is None


@pytest.mark.asyncio
async def test_propose_tool_is_scope_gated():
    """The gate sits at the tool layer: playbook_propose refuses an
    out-of-plan target before touching anything."""
    from plugin_playbooks.agent_tools import build_tools

    class _Runner:
        _tools = None

    ctx = _Ctx(ops=_Authority(_plan("plan_only", ["playbook:other"])), kind="ops")
    handlers = {td.name: h for td, h in build_tools(None, _Bus(), _Runner(), ctx)}
    out = json.loads(await handlers["playbook_propose"](name="pb"))
    assert out["gate"] == "ops_plan_scope"


def test_all_mutation_handlers_carry_the_gate():
    """Every mutation path names the gate in source — publish/rollback via
    _do_publish, edit/edit_force via _edit_impl."""
    from plugin_playbooks import agent_tools

    src = inspect.getsource(agent_tools)
    assert src.count("_ops_scope_refusal(ctx, name)") >= 6
    assert "await _ops_report_outcome(" in src  # _do_publish success tail


# ---------------------------------------------------------------- outcome


@pytest.mark.asyncio
async def test_outcome_emitted_from_ops_chat():
    bus = _Bus()
    ctx = _Ctx(ops=_Authority(), kind="ops")
    await report_outcome(ctx, bus, name="pb", facts={"action": "publish"})
    ((event, payload),) = bus.emitted
    assert event == "ops.outcome"
    assert payload == {"area_ref": "playbook:pb", "facts": {"action": "publish"}}


@pytest.mark.asyncio
async def test_outcome_not_emitted_from_building_chat():
    # a routine building-chat publish must never close an executing plan
    bus = _Bus()
    ctx = _Ctx(ops=_Authority(), kind="building")
    await report_outcome(ctx, bus, name="pb", facts={})
    assert bus.emitted == []


@pytest.mark.asyncio
async def test_outcome_not_emitted_without_ops():
    bus = _Bus()
    await report_outcome(_Ctx(ops=None, kind="ops"), bus, name="pb", facts={})
    assert bus.emitted == []


@pytest.mark.asyncio
async def test_outcome_emit_failure_never_raises():
    ctx = _Ctx(ops=_Authority(), kind="ops")
    await report_outcome(ctx, _BrokenBus(), name="pb", facts={})  # no raise


# ------------------------------------------------- fix_proposals routing


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await engine.dispose()


async def _seed_failure(sf, *, name="pb", error="KeyError: 'x'"):
    pid, rid = uuid.uuid4(), uuid.uuid4()
    async with sf() as s:
        await s.execute(insert(Playbook).values(
            id=pid, name=name, definition={}, version=1, live_version=1,
            status="enabled", manifest="# Sends the digest\nmore",
        ))
        await s.execute(insert(PlaybookRun).values(
            id=rid, playbook_id=pid, playbook_version=1, status="failed",
        ))
        await s.execute(insert(PlaybookStepRun).values(
            id=uuid.uuid4(), run_id=rid, step_id="send", step_kind="tool",
            status="failed", error=error,
        ))
        await s.commit()
    return pid, rid


def _service(sf, ctx):  # noqa: ANN001
    bus = _Bus()
    svc = FixProposalService(sf, bus, ctx)
    cards: list = []

    async def _record_card(*a, **k):  # noqa: ANN001
        cards.append(a)

    svc._post_card = _record_card
    return svc, bus, cards


@pytest.mark.asyncio
async def test_live_failure_reports_to_ops_no_card(db):
    pid, rid = await _seed_failure(db)
    svc, bus, cards = _service(db, _Ctx(ops=_Authority()))
    await svc._file_proposal_inner({"run_id": str(rid)})
    ((event, payload),) = bus.emitted
    assert event == "ops.problem_reported"
    assert payload["area_ref"] == "playbook:pb"
    assert payload["evidence"]["run_id"] == str(rid)
    assert payload["evidence"]["step"] == "send"
    assert payload["display"]["purpose"] == "Sends the digest"
    assert cards == []  # ops owns the surface — no fix-proposal card
    async with db() as s:  # ledger row still recorded (digest reads it)
        rows = (await s.execute(select(PlaybookFixProposal))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_repeat_failure_reports_again_and_bumps_ledger(db):
    pid, rid = await _seed_failure(db)
    svc, bus, cards = _service(db, _Ctx(ops=_Authority()))
    await svc._file_proposal_inner({"run_id": str(rid)})
    rid2 = uuid.uuid4()
    async with db() as s:
        await s.execute(insert(PlaybookRun).values(
            id=rid2, playbook_id=pid, playbook_version=1, status="failed",
        ))
        await s.execute(insert(PlaybookStepRun).values(
            id=uuid.uuid4(), run_id=rid2, step_id="send", step_kind="tool",
            status="failed", error="KeyError: 'x'",
        ))
        await s.commit()
    await svc._file_proposal_inner({"run_id": str(rid2)})
    assert len(bus.emitted) == 2  # repeats reported too — ops owns the counter
    assert bus.emitted[1][1]["evidence"]["run_id"] == str(rid2)
    assert cards == []
    async with db() as s:
        (row,) = (await s.execute(select(PlaybookFixProposal))).scalars().all()
        assert row.failure_count == 2


@pytest.mark.asyncio
async def test_without_ops_degrades_to_card_path(db):
    pid, rid = await _seed_failure(db)
    svc, bus, cards = _service(db, _Ctx(ops=None))
    await svc._file_proposal_inner({"run_id": str(rid)})
    assert bus.emitted == []
    assert len(cards) == 1  # 0.30.x behavior stands
