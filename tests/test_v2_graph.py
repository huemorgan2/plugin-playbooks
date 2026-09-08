"""plans/032 phase 10 — the canvas graph of a python playbook
(`plugin_playbooks/v2/graph.py`) and its routes: `GET /playbooks/{name}/graph`
and the run-trace overlay on `GET /playbooks/runs/{id}`."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import routes
from plugin_playbooks.models import Base, Playbook, PlaybookRun, PlaybookVersion
from plugin_playbooks.v2.checker import check
from plugin_playbooks.v2.graph import (
    STATUS_MAP,
    build_graph,
    node_at_line,
    parse_failed_line,
    trace_rows,
)
from plugin_playbooks.v2.journal_db import DbJournalStore

from test_v2_checker import EXAMPLE, QUEUE_EXAMPLE

NOW = datetime.now(timezone.utc)
BASE = "/api/p/plugin-playbooks"
TRIGGERS = [{"event": "manual"}]


def _graph(code: str, *, name: str = "pb", version: int = 1, triggers=TRIGGERS) -> dict:
    r = check(code, name=name, version=version)
    return build_graph(code, name=name, version=version, triggers=triggers,
                       call_sites=r.summary["call_sites"])


def _flat(block: dict) -> list[dict]:
    out = []
    for it in block["items"]:
        out.append(it)
        out += it.get("args") or []
        for key in ("then", "else", "body", "finally"):
            if it.get(key):
                out += _flat(it[key])
        for h in it.get("handlers") or []:
            out += _flat(h["body"])
    return out


def _step_ids(g: dict) -> set[str]:
    return {n for n in g["node_ids"] if n.startswith("step-")}


# ------------------------------------------------------------ build_graph
def test_graph_structure_matches_example():
    g = _graph(EXAMPLE)
    assert g["format"] == "python" and g["version"] == 1 and g["triggers"] == TRIGGERS
    assert g["node_ids"] == [
        "trigger-0", "step-rows", "compute-for-s", "for-s", "step-s",
        "compute-end-for-s", "step-approve", "step-send_message", "compute-end-run",
    ]
    root = g["root"]
    assert root["id"] == "run"
    kinds = [(it["node"], it["kind"]) for it in root["items"]]
    assert kinds == [
        ("step-rows", "tool"), ("compute-for-s", "compute"), ("for-s", "for"),
        ("step-approve", "approve"), ("step-send_message", "tool"), ("compute-end-run", "compute"),
    ]
    loop = root["items"][2]
    assert loop["label"] == "for r in good" and loop["body"]["id"] == "for-s"
    assert [it["node"] for it in loop["body"]["items"]] == ["step-s", "compute-end-for-s"]
    step = root["items"][0]
    assert step["call_site_id"] == "rows" and step["sublabel"] == "fetch_list"
    assert step["line"] == 2 and step["loop_depth"] == 0 and step["in_try"] is False
    assert root["items"][3]["kind"] == "approve"
    # every node id is unique and the pre-order flattening equals node_ids
    assert len(set(g["node_ids"])) == len(g["node_ids"])
    assert [it["node"] for it in _flat(root)] == g["node_ids"][1:]


def test_step_ids_follow_checker_call_sites():
    for code in (EXAMPLE, QUEUE_EXAMPLE):
        r = check(code, name="pb", version=1)
        g = build_graph(code, name="pb", version=1, triggers=[], call_sites=r.summary["call_sites"])
        assert _step_ids(g) == {f"step-{c['id']}" for c in r.summary["call_sites"]}


def test_duplicate_explicit_id_gets_checker_suffix():
    code = (
        "async def run(ctx, inputs):\n"
        "    a = await ctx.tool('fetch', _id='t1')\n"
        "    b = await ctx.tool('fetch', _id='t1')\n"
        "    return {'a': a, 'b': b}\n"
    )
    g = _graph(code)
    assert _step_ids(g) == {"step-t1", "step-t1_2"}
    assert g["node_ids"] == ["trigger-0", "step-t1", "step-t1_2", "compute-end-run"]


def test_ids_stable_across_compute_edits_and_change_on_rename():
    base = _graph(EXAMPLE)
    edited = EXAMPLE.replace(
        '    good = [r for r in rows["items"] if r["score"] > 3]\n',
        '    threshold = 3\n    good = [r for r in rows["items"] if r["score"] > threshold]\n',
    )
    assert edited != EXAMPLE
    g2 = _graph(edited)
    assert g2["node_ids"] == base["node_ids"]  # shifted lines, same ids
    assert [it["line"] for it in _flat(g2["root"])] != [it["line"] for it in _flat(base["root"])]
    renamed = EXAMPLE.replace("rows = await ctx.tool", "items = await ctx.tool").replace(
        'rows["items"]', 'items["items"]')
    g3 = _graph(renamed)
    assert "step-items" in g3["node_ids"] and "step-rows" not in g3["node_ids"]


def test_containers_and_compute_collapse():
    g = _graph(QUEUE_EXAMPLE)
    root = g["root"]
    assert [it["node"] for it in root["items"]] == [
        "compute-while-page", "while-page", "compute-gather-summary", "gather-summary",
        "compute-step-send_message", "step-send_message", "compute-end-run",
    ]
    head = root["items"][0]
    assert head["line"] == 2 and head["end_line"] == 4 and head["lines"] == 3
    assert head["label"] == 'queue = list(inputs["urls"])'
    loop = root["items"][1]
    assert loop["kind"] == "while" and loop["label"] == "while queue"
    body = loop["body"]["items"]
    assert [it["node"] for it in body] == ["compute-try-page", "try-page", "compute-end-while-page"]
    tb = body[1]
    assert tb["kind"] == "error_boundary" and tb["finally"] is None
    assert [it["node"] for it in tb["body"]["items"]] == ["step-page"]
    assert tb["body"]["items"][0]["in_try"] is True and tb["body"]["items"][0]["loop_depth"] == 1
    assert [h["label"] for h in tb["handlers"]] == ["except ctx.ToolError"]
    assert tb["handlers"][0]["body"]["id"] == "try-page-except-1"
    assert tb["handlers"][0]["body"]["items"][0]["node"] == "compute-end-try-page-except-1"
    # a statement without a call site is never its own node: the nested `for`
    # and `if` at lines 13-15 fold into the trailing compute of the loop body
    tail = body[2]
    assert tail["line"] == 12 and tail["end_line"] == 15 and tail["lines"] == 4
    assert node_at_line(g, 14) == "compute-end-while-page"
    assert node_at_line(g, 8) == "step-page"
    assert node_at_line(g, 10) == "compute-end-try-page-except-1"
    assert node_at_line(g, 19) == "step-summary"
    assert node_at_line(g, 24) == "compute-end-run"
    assert node_at_line(g, 99) is None


def test_if_container_has_then_and_else():
    code = (
        "async def run(ctx, inputs):\n"
        "    if inputs['x']:\n"
        "        await ctx.tool('one')\n"
        "    else:\n"
        "        await ctx.tool('two')\n"
        "        a = 1\n"
        "    return a\n"
    )
    g = _graph(code)
    cond = g["root"]["items"][0]
    assert cond["node"] == "if-one" and cond["kind"] == "if" and cond["label"] == "if inputs['x']"
    assert cond["then"]["id"] == "if-one-then" and cond["else"]["id"] == "if-one-else"
    assert [it["node"] for it in cond["then"]["items"]] == ["step-one"]
    assert [it["node"] for it in cond["else"]["items"]] == ["step-two", "compute-end-if-one-else"]
    assert g["node_ids"] == ["trigger-0", "if-one", "step-one", "step-two",
                             "compute-end-if-one-else", "compute-end-run"]


def test_gather_fans_out():
    code = (
        "async def run(ctx, inputs):\n"
        "    a, b = await ctx.gather(\n"
        "        ctx.tool('fetch', _id='t1'),\n"
        "        ctx.tool('fetch', _id='t2'),\n"
        "    )\n"
        "    return [a, b]\n"
    )
    g = _graph(code)
    # the checker lists the gather ARGUMENTS as call sites (there is no
    # journal row for the gather itself) — the fan-out node is keyed on its
    # first argument's site id
    gat = g["root"]["items"][0]
    assert gat["node"] == "gather-t1" and gat["kind"] == "gather" and gat["call_site_id"] is None
    assert gat["label"] == "gather (2)"
    assert [a["node"] for a in gat["args"]] == ["step-t1", "step-t2"]
    assert g["node_ids"] == ["trigger-0", "gather-t1", "step-t1", "step-t2", "compute-end-run"]
    assert node_at_line(g, 4) == "step-t2"
    assert node_at_line(g, 2) == "gather-t1"


def test_empty_or_broken_code_degrades():
    g = build_graph("", name="pb", version=None, triggers=None, call_sites=None)
    assert g["node_ids"] == [] and g["root"] == {"id": "run", "items": []}
    g = build_graph("def run(:\n  pass", name="pb", version=1, triggers=[], call_sites=[])
    assert g["node_ids"] == ["compute-end-run"]
    assert node_at_line(g, 2) == "compute-end-run"


# ------------------------------------------------------------ trace
def _entry(seq, sid, occ, status, **kw):
    e = {"seq": seq, "kind": "tool", "id": sid, "occurrence": occ, "name": "send",
         "args": {"n": seq}, "idempotency_key": f"r:{seq}", "status": status,
         "result": None if status != "done" else {"ok": True},
         "error": None, "attempts": [], "dry": False,
         "started_at": "2026-09-08T00:00:00+00:00", "ended_at": None, "ms": 5}
    e.update(kw)
    return e


def test_trace_rows_map_journal_onto_step_nodes():
    entries = [
        {"seq": 0, "kind": "run", "mode": "live"},
        _entry(1, "rows", 1, "done"),
        _entry(2, "send", 1, "done"),
        _entry(3, "send", 2, "failed_handled"),
        _entry(4, "send", 3, "failed", error={"type": "ToolError", "message": "boom"}),
        _entry(5, "approve", None, "parked", parked_on={"kind": "approval", "approval_id": "a1"}),
        _entry(6, "x", None, "in_flight", dry=True),
        _entry(7, "x", None, "timed_out_unknown"),
    ]
    rows = trace_rows(entries)
    assert [r["node"] for r in rows] == [
        "step-rows", "step-send", "step-send", "step-send", "step-approve", "step-x", "step-x",
    ]
    assert [r["occurrence"] for r in rows] == [1, 1, 2, 3, 1, 1, 2]
    assert [r["status"] for r in rows] == [
        "done", "done", "done", "failed", "waiting", "running", "failed"]
    assert [r["journal_status"] for r in rows][3:] == [
        "failed", "parked", "in_flight", "timed_out_unknown"]
    failed = rows[3]
    assert failed["seq"] == 4 and failed["call_site_id"] == "send"
    assert failed["error"] == {"type": "ToolError", "message": "boom"}
    assert rows[4]["parked_on"] == {"kind": "approval", "approval_id": "a1"}
    assert "parked_on" not in rows[0]
    assert rows[5]["dry"] is True and rows[0]["dry"] is False
    assert rows[0]["result"] == {"ok": True} and rows[0]["args"] == {"n": 1}
    assert set(STATUS_MAP.values()) <= {"running", "done", "failed", "waiting"}


def test_parse_failed_line():
    assert parse_failed_line("line 7: rows['x'] → KeyError: 'x' after effect fetch#1") == 7
    assert parse_failed_line("line 12: raise ValueError(...) → ValueError: no before any effect") == 12
    assert parse_failed_line("tool 'send' failed") is None
    assert parse_failed_line(None) is None
    assert parse_failed_line("") is None


# ------------------------------------------------------------ routes
class _StubRunner:
    _tools = None
    _agent = None


@pytest.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    routes.init_routes(sf, runner=_StubRunner())
    app = FastAPI()
    app.dependency_overrides[routes.get_current_user] = lambda: {"sub": "owner"}
    app.include_router(routes.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://luna.test"
    ) as c:
        yield sf, c
    await engine.dispose()


def _py_defn(code: str, name: str, version: int) -> dict:
    r = check(code, name=name, version=version)
    return {"name": name, "format": "python", "triggers": TRIGGERS, "inputs": {}, **r.summary}


def _pb_defn() -> dict:
    return {"name": "pb", "display_name": "pb", "description": "d",
            "steps": [{"id": "say", "kind": "tool_call", "tool": "send_chat_message",
                       "args": {"message": "hi"}}]}


async def _seed(sf) -> dict:
    """`pb`: v1 pblang (live), v2 python EXAMPLE (candidate); `pyonly`: v1
    python QUEUE_EXAMPLE live. Returns the ids of two v2 runs of `pb`:
    `journaled` (journal rows) and `plain` (none)."""
    async with sf() as s:
        pb = Playbook(name="pb", display_name="pb", description="d", definition=_pb_defn(),
                      code="say hi", manifest="M", version=2, live_version=1,
                      candidate_version=2, status="enabled")
        s.add(pb)
        py = Playbook(name="pyonly", display_name="pyonly", description="d",
                      definition=_py_defn(QUEUE_EXAMPLE, "pyonly", 1), code=QUEUE_EXAMPLE,
                      manifest="M", version=1, live_version=1, status="enabled",
                      format="python")
        s.add(py)
        await s.flush()
        s.add(PlaybookVersion(playbook_id=pb.id, version=1, definition=_pb_defn(), code="say hi",
                              manifest="M", author="owner", message="v1", format="pblang",
                              created_at=NOW))
        s.add(PlaybookVersion(playbook_id=pb.id, version=2, definition=_py_defn(EXAMPLE, "pb", 2),
                              code=EXAMPLE, manifest="M", author="agent", message="v2",
                              format="python", created_at=NOW))
        s.add(PlaybookVersion(playbook_id=py.id, version=1,
                              definition=_py_defn(QUEUE_EXAMPLE, "pyonly", 1),
                              code=QUEUE_EXAMPLE, manifest="M", author="agent", message="v1",
                              format="python", created_at=NOW))
        journaled = PlaybookRun(
            playbook_id=pb.id, playbook_version=2, status="failed", trigger="manual",
            is_test=True, started_at=NOW, completed_at=NOW, format="python",
            error="line 9: await ctx.tool(\"send_message\", ...) → ToolError: boom after effect send_message#1",
            error_type="ToolError", traceback="Traceback (playbook frames only)")
        plain = PlaybookRun(playbook_id=pb.id, playbook_version=2, status="done",
                            trigger="manual", is_test=True, started_at=NOW, completed_at=NOW,
                            format="python")
        v1run = PlaybookRun(playbook_id=pb.id, playbook_version=1, status="done",
                            trigger="manual", is_test=False, started_at=NOW, completed_at=NOW)
        s.add_all([journaled, plain, v1run])
        await s.commit()
        ids = {"journaled": str(journaled.id), "plain": str(plain.id), "v1": str(v1run.id)}
    store = DbJournalStore(sf)
    await store.start(ids["journaled"], {"mode": "live", "started_at": NOW.isoformat()})
    seq = await store.append_in_flight(ids["journaled"], {
        "kind": "tool", "id": "rows", "occurrence": 1, "name": "fetch_list", "args": {}})
    await store.complete(ids["journaled"], seq, {"items": []}, [], 3)
    seq = await store.append_in_flight(ids["journaled"], {
        "kind": "approve", "id": "approve", "occurrence": 1, "name": "approve", "args": {}})
    await store.complete(ids["journaled"], seq, {"approved": True}, [], 3)
    seq = await store.append_in_flight(ids["journaled"], {
        "kind": "tool", "id": "send_message", "occurrence": 1, "name": "send_message", "args": {}})
    await store.fail(ids["journaled"], seq, "ToolError", "boom", [])
    return ids


@pytest.mark.asyncio
async def test_graph_route_picks_live_then_candidate(client):
    sf, c = client
    await _seed(sf)
    # pb: live v1 is pblang → 409; explicit version=2 → python graph
    r = await c.get(f"{BASE}/playbooks/pb/graph")
    assert r.status_code == 409
    assert r.json() == {"error": "version 1 of 'pb' is pblang — the canvas builds pblang graphs client-side"}
    r = await c.get(f"{BASE}/playbooks/pb/graph", params={"version": 2})
    assert r.status_code == 200
    g = r.json()
    assert g["name"] == "pb" and g["version"] == 2 and g["format"] == "python"
    assert g["triggers"] == TRIGGERS
    assert g["node_ids"] == _graph(EXAMPLE, name="pb", version=2)["node_ids"]
    # pyonly: default = live python version
    r = await c.get(f"{BASE}/playbooks/pyonly/graph")
    assert r.status_code == 200
    assert r.json()["node_ids"] == _graph(QUEUE_EXAMPLE, name="pyonly")["node_ids"]
    assert r.json()["version"] == 1


@pytest.mark.asyncio
async def test_graph_route_404s(client):
    sf, c = client
    await _seed(sf)
    r = await c.get(f"{BASE}/playbooks/nope/graph")
    assert r.status_code == 404 and r.json()["detail"] == "Playbook 'nope' not found"
    r = await c.get(f"{BASE}/playbooks/pb/graph", params={"version": 9})
    assert r.status_code == 404 and r.json()["detail"] == "Version 9 of 'pb' not found"


@pytest.mark.asyncio
async def test_graph_route_candidate_only_and_missing_call_sites(client):
    sf, c = client
    async with sf() as s:
        defn = _py_defn(EXAMPLE, "cand", 1)
        defn.pop("call_sites")  # derived on the server when the definition lacks them
        p = Playbook(name="cand", display_name="cand", description="d", definition=defn,
                     code=EXAMPLE, manifest="M", version=1, live_version=0,
                     candidate_version=1, status="disabled", format="python")
        s.add(p)
        await s.flush()
        s.add(PlaybookVersion(playbook_id=p.id, version=1, definition=defn, code=EXAMPLE,
                              manifest="M", author="agent", message="v1", format="python",
                              created_at=NOW))
        await s.commit()
    r = await c.get(f"{BASE}/playbooks/cand/graph")
    assert r.status_code == 200
    assert r.json()["version"] == 1
    assert r.json()["node_ids"] == _graph(EXAMPLE, name="cand")["node_ids"]


@pytest.mark.asyncio
async def test_get_version_carries_format(client):
    sf, c = client
    await _seed(sf)
    r = await c.get(f"{BASE}/playbooks/pb/versions/1")
    assert r.status_code == 200 and r.json()["format"] == "pblang"
    r = await c.get(f"{BASE}/playbooks/pb/versions/2")
    assert r.status_code == 200 and r.json()["format"] == "python"


@pytest.mark.asyncio
async def test_get_run_trace_only_for_journaled_runs(client):
    sf, c = client
    ids = await _seed(sf)
    r = await c.get(f"{BASE}/playbooks/runs/{ids['journaled']}")
    assert r.status_code == 200
    body = r.json()
    assert [(t["node"], t["occurrence"], t["status"]) for t in body["trace"]] == [
        ("step-rows", 1, "done"), ("step-approve", 1, "done"), ("step-send_message", 1, "failed"),
    ]
    assert body["trace"][2]["error"] == {"type": "ToolError", "message": "boom"}
    assert body["trace"][0]["seq"] == 1 and body["trace"][0]["result"] == {"items": []}
    assert body["failed_line"] == 9
    assert body["error"].startswith("line 9:") and body["error_type"] == "ToolError"
    assert body["traceback"] == "Traceback (playbook frames only)"
    assert body["format"] == "python" and body["steps"] == []
    # journal-less runs keep the pre-phase-10 payload exactly (pblang and python alike)
    for key in ("plain", "v1"):
        body = (await c.get(f"{BASE}/playbooks/runs/{ids[key]}")).json()
        assert set(body) == {"id", "status", "trigger", "playbook_version", "inputs",
                             "format", "result", "steps"}
