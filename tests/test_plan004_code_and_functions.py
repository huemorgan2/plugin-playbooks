"""plans/004 — code() steps (jailed Python via plugin-inline-code-run) and
def functions (inline macro expansion).

code(): a first-class step kind whose Python body runs in the inline-code-run
kernel jail (JSON mode — `inputs` dict in, `return` value out). The runner
delegates through the tool registry (`code_run`), never a Python import.

def: top-level functions expand at each call site — every id/var the body
defines gets a per-call prefix, and references (compiled Jinja and raw Jinja
alike) are rewritten to match. Functions are procedures: no return value.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.definition import StepKind
from plugin_playbooks.models import Base, Playbook, PlaybookStepRun
from plugin_playbooks.pblang import (
    PlaybookCompileError,
    compile_playbook,
    defs_equal,
    generate_code,
)
from plugin_playbooks.probes import collect_tools
from plugin_playbooks.runner import PlaybookRunner
from plugin_playbooks.validation import validate_definition


def _errors_of(src: str) -> list[str]:
    with pytest.raises(PlaybookCompileError) as ei:
        compile_playbook(src, name="t")
    return [i.message for i in ei.value.issues]


# ------------------------------------------------------------- code() steps


def test_code_step_compiles_and_round_trips():
    src = (
        "playbook(name='t')\n"
        "norm = code('''\n"
        "digits = ''.join(c for c in inputs['raw'] if c.isdigit())\n"
        "return {'phone': digits}\n"
        "''', inputs={'raw': inputs.raw})\n"
    )
    pb = compile_playbook(src, name="t")
    s = pb.steps[0]
    assert s.kind == StepKind.CODE
    assert s.id == "norm"
    assert "isdigit" in s.source
    assert s.code_inputs == {"raw": "{{ inputs.raw }}"}
    assert defs_equal(pb, compile_playbook(generate_code(pb), name="t"))


def test_code_step_syntax_error_caught_at_compile_time():
    msgs = _errors_of(
        "playbook(name='t')\n"
        "bad = code('x = = 1')\n"
    )
    assert any("Python syntax error" in m for m in msgs)


def test_code_step_source_must_be_string_literal():
    msgs = _errors_of(
        "playbook(name='t')\n"
        "bad = code(inputs.body)\n"
    )
    assert any("string literal" in m for m in msgs)


def test_code_step_validation_keys_and_probe_dependency():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "x = code('return 1', inputs={'n': 2})\n",
        name="t",
    )
    defn = pb.model_dump(mode="json", exclude_none=True, by_alias=True)
    assert collect_tools(defn) == ["code_run"]

    class _Reg:
        def __init__(self, names):
            self._names = names

        def get(self, name):
            if name not in self._names:
                raise KeyError(name)
            return object()

    issues = validate_definition(defn, tool_registry=_Reg({"code_run"}))
    assert not [i for i in issues if i.severity == "error"]
    missing = validate_definition(defn, tool_registry=_Reg(set()))
    warn = [i for i in missing if "code_run" in i.message]
    assert warn and warn[0].severity == "warning"


# --------------------------------------------------------- code() at runtime


class _Bus:
    async def emit(self, name, payload):
        pass


class _Tool:
    def __init__(self, handler):
        self.handler = handler


class _Tools:
    def __init__(self, **tools):
        self._tools = tools

    def get(self, name):
        return self._tools[name]


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await engine.dispose()


def _code_pb(name="p", source="return 1", code_inputs=None):
    return Playbook(
        name=name, display_name=name, status="enabled",
        definition={"name": name, "steps": [
            {"id": "calc", "kind": "code", "source": source,
             "code_inputs": code_inputs or {}},
        ]},
    )


async def _save(sf, pb):
    async with sf() as s:
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
    return pb


async def _step_row(sf, run_id, step_id):
    async with sf() as s:
        return (await s.execute(
            select(PlaybookStepRun).where(
                PlaybookStepRun.run_id == run_id,
                PlaybookStepRun.step_id == step_id,
            )
        )).scalar_one()


@pytest.mark.asyncio
async def test_code_step_live_delegates_to_code_run_tool(db):
    sf = db
    seen = {}

    async def fake_code_run(**kw):
        seen.update(kw)
        return {"ok": True, "exit_code": 0, "stdout": "hi\n", "stderr": "",
                "timed_out": False, "result": {"phone": "0521"}}

    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(code_run=_Tool(fake_code_run)),
        events=_Bus(),
    )
    pb = await _save(sf, _code_pb(
        source="return {'phone': '0521'}",
        code_inputs={"raw": "{{ inputs.raw }}"},
    ))
    run = await runner.start_run(pb, inputs={"raw": "052-1"})
    assert run.status == "done"
    assert seen["code"] == "return {'phone': '0521'}"
    assert seen["input_json"] == {"raw": "052-1"}
    assert seen["title"] == "playbook code step 'calc'"
    out = (await _step_row(sf, run.id, "calc")).outputs
    assert out["result"] == {"phone": "0521"}
    assert out["stdout"] == "hi\n"


@pytest.mark.asyncio
async def test_code_step_failure_surfaces_stderr(db):
    sf = db

    async def fake_code_run(**kw):
        return {"ok": False, "exit_code": 1, "stdout": "", "timed_out": False,
                "stderr": "Traceback...\nKeyError: 'raw'"}

    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(code_run=_Tool(fake_code_run)),
        events=_Bus(),
    )
    pb = await _save(sf, _code_pb())
    run = await runner.start_run(pb, inputs={})
    assert run.status == "failed"
    row = await _step_row(sf, run.id, "calc")
    assert "KeyError" in (row.error or "")


@pytest.mark.asyncio
async def test_code_step_result_error_fails_the_step(db):
    sf = db

    async def fake_code_run(**kw):
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "",
                "timed_out": False,
                "result_error": "code did not produce outputs/result.json"}

    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(code_run=_Tool(fake_code_run)),
        events=_Bus(),
    )
    pb = await _save(sf, _code_pb())
    run = await runner.start_run(pb, inputs={})
    assert run.status == "failed"
    row = await _step_row(sf, run.id, "calc")
    assert "result.json" in (row.error or "")


@pytest.mark.asyncio
async def test_code_step_without_plugin_fails_with_install_hint(db):
    sf = db
    runner = PlaybookRunner(
        session_factory=sf, tool_registry=_Tools(), events=_Bus(),
    )
    pb = await _save(sf, _code_pb())
    run = await runner.start_run(pb, inputs={})
    assert run.status == "failed"
    row = await _step_row(sf, run.id, "calc")
    assert "plugin-inline-code-run" in (row.error or "")


@pytest.mark.asyncio
async def test_code_step_dry_run_stubs_by_id(db):
    sf = db
    runner = PlaybookRunner(
        session_factory=sf, tool_registry=_Tools(), events=_Bus(),
    )
    pb = _code_pb(code_inputs={"raw": "{{ inputs.raw }}"})
    out = await runner.dry_run(pb, inputs={"raw": "052"},
                               stubs={"calc": {"phone": "0521"}})
    assert out["status"] == "done"
    step = out["trace"][0]["output"]
    assert step["stubbed"] is True
    assert step["result"] == {"phone": "0521"}
    assert step["resolved_inputs"] == {"raw": "052"}
    # unstubbed: never touches the jail, result is a dry marker
    out2 = await runner.dry_run(pb, inputs={"raw": "052"})
    assert out2["trace"][0]["output"]["result"] == {"_dry": True}


# ------------------------------------------------------------ def functions


FN_SRC = """
playbook(name='t', inputs={'type': 'object',
                           'properties': {'who': {'type': 'string'}}})

def notify(target, note):
    msg = f"hello {target}: {note}"
    sent = tool('send_message', to=target, text=msg)

start = tool('fetch', q=inputs.who)
notify(inputs.who, 'first')
notify('ops', note=f'second {start.output}')
"""


def test_function_expands_per_call_with_unique_prefixes():
    pb = compile_playbook(FN_SRC, name="t")
    ids = [s.id for s in pb.steps]
    assert ids == [
        "start",
        "notify__target", "notify__note", "notify__msg", "notify__sent",
        "notify_2__target", "notify_2__note", "notify_2__msg", "notify_2__sent",
    ]
    # args become state sets in the CALLER's scope
    assert pb.steps[1].state[0].value == "{{ inputs.who }}"
    assert pb.steps[6].state[0].value == "second {{ steps.start.output }}"
    # body references are renamed per call
    assert pb.steps[4].args == {"to": "{{ vars.notify__target }}",
                                "text": "{{ vars.notify__msg }}"}
    assert pb.steps[8].args == {"to": "{{ vars.notify_2__target }}",
                                "text": "{{ vars.notify_2__msg }}"}


def test_function_expansion_round_trips():
    pb = compile_playbook(FN_SRC, name="t")
    assert defs_equal(pb, compile_playbook(generate_code(pb), name="t"))


def test_function_call_inside_nested_step_list():
    src = (
        "playbook(name='t')\n"
        "def ping(target):\n"
        "    x = tool('ping', to=target)\n"
        "gate = if_(inputs.go, then=[\n"
        "    ping('a'),\n"
        "    ping('b'),\n"
        "])\n"
    )
    pb = compile_playbook(src, name="t")
    then = pb.steps[0].then
    assert [s.id for s in then] == [
        "ping__target", "ping__x", "ping_2__target", "ping_2__x",
    ]
    assert defs_equal(pb, compile_playbook(generate_code(pb), name="t"))


def test_function_body_reads_outer_steps_and_vars():
    src = (
        "playbook(name='t')\n"
        "base = tool('fetch')\n"
        "n = 3\n"
        "def f():\n"
        "    x = base.output + n\n"
        "f()\n"
    )
    pb = compile_playbook(src, name="t")
    assert pb.steps[2].state[0].value == "{{ steps.base.output + vars.n }}"


def test_code_step_inside_function_renames_inputs_not_source():
    src = (
        "playbook(name='t')\n"
        "def crunch(raw):\n"
        "    out = code('return inputs[\"raw\"]', inputs={'raw': raw})\n"
        "crunch('abc')\n"
    )
    pb = compile_playbook(src, name="t")
    step = pb.steps[1]
    assert step.kind == StepKind.CODE
    assert step.id == "crunch__out"
    # the Jinja input ref is renamed; the jail Python is untouched
    assert step.code_inputs == {"raw": "{{ vars.crunch__raw }}"}
    assert step.source == 'return inputs["raw"]'


def test_function_error_cases():
    hdr = "playbook(name='t')\n"
    cases = [
        (hdr + "def f(a):\n    x = a\nf()", "missing argument"),
        (hdr + "def f(a):\n    x = a\nf(1, 2)", "takes 1 argument"),
        (hdr + "def f(a):\n    x = a\nf(1, b=2)", "no parameter 'b'"),
        (hdr + "def f(a):\n    x = a\nf(1, a=1)", "multiple values"),
        (hdr + "def f():\n    f()\nf()", "Recursive function call"),
        (hdr + "def f():\n    return 1\nf()", "return is not allowed"),
        (hdr + "def f(a):\n    x = a\ny = f(1)", "no return value"),
        (hdr + "def f(a=1):\n    x = a\n", "plain positional"),
        (hdr + "def f():\n    def g():\n        x = 1\nf()", "Nested function"),
        (hdr + "def tool(a):\n    x = a\n", "shadows a built-in"),
        (hdr + "f()\ndef f():\n    x = 1\n", "Unknown step function"),
        (hdr + "def f():\n    x = 1\nz = tool('t', id='f')", "MISMATCH"),
        (hdr + "f__x = tool('t')\ndef f():\n    x = 1\nf()", "Duplicate step id"),
        (hdr + "a = tool('t')\ndef f(a):\n    x = a\nf(1)", "Duplicate step id"),
    ]
    for src, frag in cases:
        if frag == "MISMATCH":
            # explicit id colliding with a function name
            msgs = _errors_of(hdr + "def f():\n    x = 1\ntool('t', id='f')")
            assert any("collides with a function name" in m for m in msgs), msgs
            continue
        msgs = _errors_of(src)
        assert any(frag in m for m in msgs), (frag, msgs)


def test_function_call_depth_capped():
    hdr = "playbook(name='t')\n"
    chain = "\n".join(
        f"def f{i}():\n    f{i - 1}()" if i else "def f0():\n    x = 1"
        for i in range(10)
    )
    msgs = _errors_of(hdr + chain + "\nf9()")
    assert any("nested too deeply" in m for m in msgs)


def test_unused_function_is_fine_and_body_never_compiled():
    # an unused def costs nothing — its body is only compiled on expansion
    pb = compile_playbook(
        "playbook(name='t')\ndef f():\n    x = 1\ny = tool('t')", name="t",
    )
    assert [s.id for s in pb.steps] == ["y"]
