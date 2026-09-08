"""plans/032 phase 09 — shared harness for the provenance / overview tests.

`_RowRunner` writes a REAL `PlaybookRun` row for every start (the way
`runner.start_run_background` stamps `playbook_version = live_version or
version`), so the tools derive the envelope from a row, never from a fake.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from evidence import EXPLANATION, green_run
from readstage import parse_read_stage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_repro_fixplaybooks_lifecycle import CODE, NEW_CODE, _Approvals, _Bus, _Ctx

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base, PlaybookRun

TERMINAL = ("done", "failed", "cancelled", "timed_out_unknown")


class _RowRunner:
    _tools = None
    _agent = None

    def __init__(self, sf) -> None:
        self.sf = sf
        self.status = "done"          # what a started run ends in
        self.dry_status = "done"      # what the v1 dry run reports
        self.started: list = []
        self._v2 = SimpleNamespace(dry_run=self._dry_run_v2)

    async def start_run_background(
        self, playbook, inputs=None, trigger=None, is_test=False, **kw,
    ):
        self.started.append((playbook, trigger, is_test, kw))
        now = datetime.now(timezone.utc)
        row = PlaybookRun(
            playbook_id=playbook.id,
            playbook_version=playbook.live_version or playbook.version,
            trigger=trigger, is_test=is_test, inputs=inputs or {},
            status=self.status, started_at=now,
            completed_at=now + timedelta(seconds=1) if self.status in TERMINAL else None,
            format=getattr(playbook, "format", None) or "pblang",
            error="boom" if self.status == "failed" else None,
        )
        async with self.sf() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
        return row

    async def wait_for_run(self, run_id, timeout=None):
        async with self.sf() as s:
            return await s.get(PlaybookRun, run_id)

    async def dry_run(self, playbook, inputs=None, stubs=None):
        return {"dry_run": True, "banner": "SIMULATED", "status": self.dry_status, "trace": []}

    async def _dry_run_v2(self, playbook, inputs=None, stubs=None, version=None):
        return {"status": "simulated", "steps_ran": {}, "unreached_call_sites": []}


async def make_env(ctx=None, approvals=None):
    approvals = approvals or _Approvals()
    ctx = ctx or _Ctx(approvals)
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    runner = _RowRunner(sf)
    tools = {td.name: h for td, h in build_tools(sf, _Bus(), runner, ctx)}
    return SimpleNamespace(sf=sf, runner=runner, tools=tools, approvals=approvals, ctx=ctx)


async def call(env, tool: str, **kw) -> dict:
    return json.loads(await env.tools[tool](**kw))


async def publish_v1(env, name: str = "greeter", *, autonomy: str = "agent_may_trigger") -> None:
    """propose → green candidate run of v1 → publish: live v1, no candidate."""
    out = await call(env, "playbook_propose", name=name, code=CODE.replace("greeter", name),
                     agent_autonomy=autonomy)
    assert out["status"] == "candidate_saved", out
    await green_run(env.sf, 1, name=name)
    out = await call(env, "playbook_publish", name=name, explanation=EXPLANATION)
    assert out["status"] == "published", out


async def add_candidate(env, name: str = "greeter") -> int:
    """edit → candidate v(live+1) with a green test run."""
    read = parse_read_stage(await env.tools["playbook_edit"](name=name))
    out = await call(env, "playbook_edit", name=name, ticket=read["ticket"],
                     code=NEW_CODE.replace("greeter", name))
    assert out["status"] == "candidate_saved", out
    await green_run(env.sf, out["candidate_version"], name=name)
    return out["candidate_version"]


async def live_with_candidate(env, name: str = "greeter") -> None:
    """live v1 + candidate v2 (the lifecycle tests' `_green_candidate`)."""
    await publish_v1(env, name)
    assert await add_candidate(env, name) == 2
