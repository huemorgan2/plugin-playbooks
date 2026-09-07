"""plans/032 phase 04 — loud intake (docs/v2.md §2 Language): a declared
number input arrives in the run coerced (the trigger map renders strings;
the chat path passes what the agent typed); a value that cannot become the
declared type fails AT INTAKE, naming the input and the type — on the chat
path as a rejection without a run row, on the trigger path as a failed run
row the digest can show. v1 gets the same intake.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from evidence import EXPLANATION, green_run
from sqlalchemy import select
from v2harness import _effect, env

from plugin_playbooks.models import Playbook, PlaybookRun
from plugin_playbooks.runner import InputTypeError, _coerce_inputs
from plugin_playbooks.triggers import PlaybookTriggerService

SCHEMA = '{"type": "object", "properties": {"n": {"type": "number"}}}'

PY_ECHO = '''async def run(ctx, inputs):
    got = await ctx.tool("echo", n=inputs["n"])
    return {"n": inputs["n"], "got": got}
'''

V1_ECHO = (
    "playbook(name='v1echo', inputs={'type': 'object', "
    "'properties': {'n': {'type': 'number'}}})\n"
    "got = tool('echo', n=inputs.n)\n"
)


def _script(env: dict) -> dict:
    """First spawn: the echo effect carrying the input the loop handed the
    playbook (journal[0].inputs); second spawn: return."""
    journal = env["journal"]
    if len(journal) == 1:
        return _effect(1, "got", 1, "tool", "echo", {"n": journal[0]["inputs"]["n"]})
    return {"kind": "return", "value": {"n": journal[0]["inputs"]["n"]}}


class _Echo:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kw):
        self.calls.append(kw)
        return {"ok": True}


async def _live_python(e, *, triggers: str | None = None) -> Playbook:
    kw = {"triggers": triggers} if triggers else {}
    out = json.loads(await e.tools["playbook_propose"](
        name="pyecho", code=PY_ECHO, inputs_schema=SCHEMA,
        agent_autonomy="agent_may_trigger", **kw,
    ))
    assert out["status"] == "candidate_saved", out
    await green_run(e.sf, 1, name="pyecho")
    pub = json.loads(await e.tools["playbook_publish"](name="pyecho", explanation=EXPLANATION))
    assert pub["status"] == "published", pub
    async with e.sf() as s:
        return (await s.execute(select(Playbook).where(Playbook.name == "pyecho"))).scalar_one()


async def _runs(sf) -> list[PlaybookRun]:
    async with sf() as s:
        return list((await s.execute(
            select(PlaybookRun).where(PlaybookRun.trigger != "agent-candidate")
        )).scalars().all())


async def test_number_string_reaches_run_as_4_on_chat_path():
    echo = _Echo()
    e = await env(script=_script, echo=echo)
    try:
        await _live_python(e)
        out = json.loads(await e.tools["playbook_run"](
            name="pyecho", inputs='{"n": "4"}', wait_seconds=5,
        ))
        assert out["status"] == "done", out
        assert echo.calls == [{"n": 4}]
        assert not isinstance(echo.calls[0]["n"], str)
        runs = await _runs(e.sf)
        assert len(runs) == 1 and runs[0].inputs["n"] == 4
        assert not isinstance(runs[0].inputs["n"], str)
    finally:
        await e.dispose()


async def _fire(e, svc: PlaybookTriggerService, payload: dict) -> None:
    handlers = e.bus.handlers["x"]
    assert len(handlers) == 1
    await handlers[0](payload)
    await asyncio.gather(*list(e.runner._tasks.values()))


async def test_number_string_reaches_run_as_4_on_trigger_path():
    echo = _Echo()
    e = await env(script=_script, echo=echo)
    try:
        await _live_python(e, triggers='[{"event": "x", "map": {"n": "{{ event.payload.n }}"}}]')
        svc = PlaybookTriggerService(e.sf, e.bus, e.runner)
        await svc.start()
        assert set(svc._unsubs) == {"x"}
        await _fire(e, svc, {"n": 4})
        assert echo.calls == [{"n": 4}]
        assert not isinstance(echo.calls[0]["n"], str)
        runs = await _runs(e.sf)
        assert len(runs) == 1 and runs[0].status == "done"
        assert runs[0].inputs["n"] == 4 and not isinstance(runs[0].inputs["n"], str)
        assert svc._in_flight == set()
    finally:
        await e.dispose()


async def test_uncoercible_input_fails_at_intake_on_chat_path():
    echo = _Echo()
    e = await env(script=_script, echo=echo)
    try:
        await _live_python(e)
        out = json.loads(await e.tools["playbook_run"](
            name="pyecho", inputs='{"n": "abc"}', wait_seconds=5,
        ))
        assert out["status"] == "rejected"
        assert out["error"] == "input 'n' expects number, got 'abc'"
        assert out["input"] == "n" and out["expected"] == "number"
        assert await _runs(e.sf) == []
        assert echo.calls == []
        assert e.code_run.calls == []
        # the candidate path rejects the same way
        read = await e.tools["playbook_edit"](name="pyecho")
        ticket = json.loads(read.split("\n", 1)[0])["ticket"]
        saved = json.loads(await e.tools["playbook_edit"](
            name="pyecho", ticket=ticket, code=PY_ECHO + "\n# v2\n",
        ))
        assert saved["status"] == "candidate_saved", saved
        cand = json.loads(await e.tools["playbook_run_candidate"](
            name="pyecho", inputs='{"n": "abc"}', wait_seconds=5,
        ))
        assert cand["status"] == "rejected" and cand["input"] == "n"
        assert cand["candidate_version"] == 2
        assert echo.calls == [] and e.code_run.calls == []
    finally:
        await e.dispose()


async def test_uncoercible_input_fails_at_intake_on_trigger_path():
    echo = _Echo()
    e = await env(script=_script, echo=echo)
    try:
        await _live_python(e, triggers='[{"event": "x", "map": {"n": "{{ event.payload.n }}"}}]')
        svc = PlaybookTriggerService(e.sf, e.bus, e.runner)
        await svc.start()
        await _fire(e, svc, {"n": "abc"})
        runs = await _runs(e.sf)
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "failed"
        assert run.error_type == "InputTypeError"
        assert "'n'" in run.error and "number" in run.error
        assert run.trigger == "x" and run.failed_at is not None
        assert echo.calls == [] and e.code_run.calls == []
        assert svc._in_flight == set()
        # the run list and the digest read it
        listed = json.loads(await e.tools["playbook_runs"](name="pyecho", status="failed"))
        assert listed["runs"][0]["error_type"] == "InputTypeError"
        assert listed["runs"][0]["error"] == run.error
    finally:
        await e.dispose()


async def test_v1_playbook_gets_the_same_loud_intake():
    echo = _Echo()
    e = await env(echo=echo)
    try:
        out = json.loads(await e.tools["playbook_propose"](
            name="v1echo", code=V1_ECHO, agent_autonomy="agent_may_trigger",
        ))
        assert out["status"] == "candidate_saved" and out["format"] == "pblang", out
        await green_run(e.sf, 1, name="v1echo")
        pub = json.loads(await e.tools["playbook_publish"](name="v1echo", explanation=EXPLANATION))
        assert pub["status"] == "published", pub

        rejected = json.loads(await e.tools["playbook_run"](
            name="v1echo", inputs='{"n": "abc"}', wait_seconds=5,
        ))
        assert rejected["status"] == "rejected"
        assert rejected["error"] == "input 'n' expects number, got 'abc'"
        assert rejected["input"] == "n" and rejected["expected"] == "number"
        assert await _runs(e.sf) == [] and echo.calls == []

        ok = json.loads(await e.tools["playbook_run"](
            name="v1echo", inputs='{"n": "4"}', wait_seconds=5,
        ))
        assert ok["status"] == "done", ok
        assert len(echo.calls) == 1
        # the intake stored the coerced value (v1's step args still go
        # through Jinja rendering, which is v1's own, unchanged contract)
        runs = await _runs(e.sf)
        assert len(runs) == 1 and runs[0].inputs["n"] == 4
        assert not isinstance(runs[0].inputs["n"], str)
    finally:
        await e.dispose()


def test_coerce_inputs_raises_typed_error():
    pb = Playbook(
        name="x", display_name="x", definition={"name": "x", "steps": []},
        inputs_schema=json.loads(SCHEMA),
    )
    with pytest.raises(InputTypeError) as ei:
        _coerce_inputs(pb, {"n": "abc"})
    assert (ei.value.input, ei.value.expected, ei.value.got) == ("n", "number", "abc")
    assert str(ei.value) == "input 'n' expects number, got 'abc'"
    assert isinstance(ei.value, ValueError)
    assert _coerce_inputs(pb, {"n": "4"}) == {"n": 4.0}
