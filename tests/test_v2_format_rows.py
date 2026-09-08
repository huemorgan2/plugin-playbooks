"""plans/032 phase 08 (part 1) — `format` per VERSION ROW and per RUN ROW:
the migration + backfill, mint/run stamping, a python candidate beside a
pblang live version running each under its own runtime, and an edit that
changes the language (docs/v2.md §9).
"""

from __future__ import annotations

import json

from evidence import EXPLANATION, green_run
from readstage import parse_read_stage
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from v2harness import CODE, _effect, env

from plugin_playbooks import _ensure_columns, backfill_format
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookStepRun, PlaybookVersion
from plugin_playbooks.versioning import mint_version

PY_GREETER = (
    "async def run(ctx, inputs):\n"
    "    say = await ctx.tool('echo', message=inputs['greeting'])\n"
    "    return say\n"
)


async def _echo(**kw):
    return kw


async def _row(sf, name) -> Playbook:
    async with sf() as s:
        return (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()


async def _versions(sf, name) -> list[PlaybookVersion]:
    async with sf() as s:
        pb = (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()
        return list((await s.execute(
            select(PlaybookVersion).where(PlaybookVersion.playbook_id == pb.id)
            .order_by(PlaybookVersion.version)
        )).scalars().all())


async def _runs(sf, name) -> list[PlaybookRun]:
    async with sf() as s:
        pb = (await s.execute(select(Playbook).where(Playbook.name == name))).scalar_one()
        return list((await s.execute(
            select(PlaybookRun).where(PlaybookRun.playbook_id == pb.id)
            .order_by(PlaybookRun.started_at)
        )).scalars().all())


# ------------------------------------------------------------------ 1
async def test_columns_migrate_and_backfill():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as s:
            py = Playbook(name="py", display_name="py", format="python", code="async def run(ctx, inputs):\n    return 1\n",
                          definition={"name": "py", "format": "python"})
            pb = Playbook(name="pb", display_name="pb", definition={"name": "pb", "steps": []})
            s.add_all([py, pb])
            await s.commit()
            for p in (py, pb):
                s.add(PlaybookVersion(playbook_id=p.id, version=1, definition=p.definition,
                                      code=p.code, author="owner", message="v1"))
                s.add(PlaybookRun(playbook_id=p.id, playbook_version=1, status="done", trigger="agent"))
            await s.commit()
        # an install that predates phase 08: no format on the row tables, no result
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE playbook_versions DROP COLUMN format"))
            await conn.execute(text("ALTER TABLE playbook_runs DROP COLUMN format"))
            await conn.execute(text("ALTER TABLE playbook_runs DROP COLUMN result"))

        def _cols(table):
            def inner(sync_conn):
                return [c["name"] for c in inspect(sync_conn).get_columns(table)]
            return inner

        async with engine.connect() as conn:
            assert "format" not in await conn.run_sync(_cols("playbook_versions"))
            runs_cols = await conn.run_sync(_cols("playbook_runs"))
            assert "format" not in runs_cols and "result" not in runs_cols
        await _ensure_columns(engine)
        async with engine.connect() as conn:
            v_after = await conn.run_sync(_cols("playbook_versions"))
            r_after = await conn.run_sync(_cols("playbook_runs"))
        assert "format" in v_after
        assert "format" in r_after and "result" in r_after
        # migrated rows read the DEFAULT — pblang for everything, result null
        async with sf() as s:
            vrows = (await s.execute(select(PlaybookVersion))).scalars().all()
            rrows = (await s.execute(select(PlaybookRun))).scalars().all()
        assert {v.format for v in vrows} == {"pblang"}
        assert {r.format for r in rrows} == {"pblang"}
        assert all(r.result is None for r in rrows)
        # the backfill stamps the python playbook's rows from the parent
        assert await backfill_format(sf) == 2
        for name, fmt in (("py", "python"), ("pb", "pblang")):
            assert [v.format for v in await _versions(sf, name)] == [fmt]
            assert [r.format for r in await _runs(sf, name)] == [fmt]
        assert await backfill_format(sf) == 0  # idempotent
        await _ensure_columns(engine)  # idempotent: nothing added twice
        async with engine.connect() as conn:
            assert await conn.run_sync(_cols("playbook_versions")) == v_after
            assert await conn.run_sync(_cols("playbook_runs")) == r_after
    finally:
        await engine.dispose()


# ------------------------------------------------------------------ 2
async def test_mint_and_run_stamp_format():
    e = await env(echo=_echo)
    try:
        for name, code, fmt in (("py", PY_GREETER, "python"), ("pb", CODE.replace("greeter", "pb"), "pblang")):
            out = json.loads(await e.tools["playbook_propose"](name=name, code=code))
            assert out["status"] == "candidate_saved" and out["format"] == fmt, out
            assert [v.format for v in await _versions(e.sf, name)] == [fmt]
            out = json.loads(await e.tools["playbook_run_candidate"](
                name=name, inputs='{"greeting": "hi"}', wait_seconds=10,
            ))
            assert out["status"] == "done", out
            runs = await _runs(e.sf, name)
            assert [r.format for r in runs] == [fmt]
        # mint_version(format=) stamps the row explicitly; the default is the
        # playbook's own format
        async with e.sf() as s:
            pb = (await s.execute(select(Playbook).where(Playbook.name == "pb"))).scalar_one()
            v2 = await mint_version(
                s, pb, definition=pb.definition, code=pb.code, manifest="",
                author="owner", message="explicit", format="python",
            )
            v3 = await mint_version(
                s, pb, definition=pb.definition, code=pb.code, manifest="",
                author="owner", message="default",
            )
            await s.commit()
            assert (v2.format, v3.format) == ("python", "pblang")
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 3
async def test_python_candidate_beside_pblang_live_runs_each_under_its_runtime(tmp_path):
    def script(envelope):
        if len(envelope["journal"]) == 1:
            return _effect(1, "say", 1, "tool", "echo", {"message": envelope["journal"][0]["inputs"]["greeting"]})
        return {"kind": "return", "value": envelope["journal"][1]["result"]}

    e = await env(script=script, echo=_echo)
    try:
        out = json.loads(await e.tools["playbook_propose"](
            name="greeter", code=CODE, agent_autonomy="agent_may_trigger",
        ))
        assert out["status"] == "candidate_saved", out
        await green_run(e.sf, 1)
        out = json.loads(await e.tools["playbook_publish"](name="greeter", explanation=EXPLANATION))
        assert out["status"] == "published", out
        read = parse_read_stage(await e.tools["playbook_edit"](name="greeter"))
        out = json.loads(await e.tools["playbook_edit"](name="greeter", ticket=read["ticket"], code=PY_GREETER))
        assert out["status"] == "candidate_saved" and out["format"] == "python", out
        assert [v.format for v in await _versions(e.sf, "greeter")] == ["pblang", "python"]

        # live = v1 pblang: the v1 runner executes it (no code_run call)
        live = json.loads(await e.tools["playbook_run"](
            name="greeter", inputs='{"greeting": "hi"}', wait_seconds=10,
        ))
        assert live["status"] == "done", live
        assert e.code_run.calls == []
        assert live["step_results"]["say"]["tool"] == "echo"
        assert live["result"] is None
        # candidate = v2 python: the segment loop executes it (code_run)
        cand = json.loads(await e.tools["playbook_run_candidate"](
            name="greeter", inputs='{"greeting": "yo"}', wait_seconds=10,
        ))
        assert cand["status"] == "done", cand
        assert len(e.code_run.calls) == 2
        assert cand["result"] == {"message": "yo"}

        runs = await _runs(e.sf, "greeter")
        by_id = {str(r.id): r for r in runs}
        r_live, r_cand = by_id[live["run_id"]], by_id[cand["run_id"]]
        assert (r_live.playbook_version, r_live.format, r_live.result) == (1, "pblang", None)
        assert (r_cand.playbook_version, r_cand.format, r_cand.result) == (2, "python", {"message": "yo"})
        async with e.sf() as s:
            steps = (await s.execute(
                select(PlaybookStepRun).where(PlaybookStepRun.run_id == r_cand.id)
            )).scalars().all()
        assert [s.step_id for s in steps] == ["say#1"]  # v2 step rows, per occurrence
        # the completed events carry each run's own result
        done = {p["run_id"]: p for p in e.bus.named("playbook.run.completed")}
        assert done[live["run_id"]]["result"] is None
        assert done[cand["run_id"]]["result"] == {"message": "yo"}
    finally:
        await e.dispose()


# ------------------------------------------------------------------ 4
async def test_edit_may_change_format():
    """The rows behind tests/test_v2_format_tools.py::test_edit_may_change_format:
    the candidate VERSION ROW carries the new language, the playbook row keeps
    the live one until publish, and a rollback restores the old language."""
    e = await env(echo=_echo)
    try:
        out = json.loads(await e.tools["playbook_propose"](name="g", code=CODE.replace("greeter", "g")))
        assert out["status"] == "candidate_saved", out
        await green_run(e.sf, 1, name="g")
        assert json.loads(await e.tools["playbook_publish"](name="g", explanation=EXPLANATION))["status"] == "published"
        read = parse_read_stage(await e.tools["playbook_edit"](name="g"))
        out = json.loads(await e.tools["playbook_edit"](name="g", ticket=read["ticket"], code=PY_GREETER))
        assert out["status"] == "candidate_saved", out
        assert (out["format"], out["live_format"]) == ("python", "pblang")
        assert "candidate v2 is python; live v1 stays pblang until publish" in out["next"]
        assert [v.format for v in await _versions(e.sf, "g")] == ["pblang", "python"]
        pb = await _row(e.sf, "g")
        assert (pb.live_version, pb.candidate_version, pb.format) == (1, 2, "pblang")
        # a second edit of the candidate defaults to the CANDIDATE's format
        read = parse_read_stage(await e.tools["playbook_edit"](name="g"))
        assert (read["format"], read["live_format"]) == ("python", "pblang")
        out = json.loads(await e.tools["playbook_edit"](
            name="g", ticket=read["ticket"], code=PY_GREETER.replace("return say", "return {'said': say}"),
        ))
        assert out["status"] == "candidate_saved" and out["format"] == "python", out
        assert [v.format for v in await _versions(e.sf, "g")] == ["pblang", "python", "python"]
        # publish flips the playbook's language; rollback flips it back
        await green_run(e.sf, 3, name="g")
        out = json.loads(await e.tools["playbook_publish"](name="g", explanation=EXPLANATION))
        assert out["status"] == "published", out
        pb = await _row(e.sf, "g")
        assert (pb.live_version, pb.format) == (3, "python")
        out = json.loads(await e.tools["playbook_rollback"](name="g"))
        assert out.get("error") is None, out
        pb = await _row(e.sf, "g")
        assert (pb.live_version, pb.format) == (1, "pblang")
    finally:
        await e.dispose()
