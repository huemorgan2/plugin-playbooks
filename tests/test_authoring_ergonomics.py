"""0.15.0 (plans/003) — authoring ergonomics.

Phase 1: item-first template resolution (dict `.attr` always reads the key).
Phase 2: regex/split filter kit.
Phase 3: value assignment `x = expr` → state set op, bare-name → vars.x.
Phase 4: the language reference is recallable (edit read stage, failed
validate, dedicated tool).
"""

from __future__ import annotations

import json

import pytest

from readstage import parse_read_stage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base
from plugin_playbooks.pblang.compiler import PlaybookCompileError, compile_playbook
from plugin_playbooks.reference import LANGUAGE_CHEATSHEET
from plugin_playbooks.runner import _SANDBOX_ENV


# ---------------------------------------------------------------- phase 1

def test_dict_attr_reads_key_at_depth():
    data = {"steps": {"fetch": {"result": {"items": [1, 2], "keys": "k", "values": 3}}}}
    assert _SANDBOX_ENV.from_string(
        "{{ steps.fetch.result.items | length }}").render(**data) == "2"
    assert _SANDBOX_ENV.from_string(
        "{{ steps.fetch.result.keys }}").render(**data) == "k"
    assert _SANDBOX_ENV.from_string(
        "{{ steps.fetch.result.values }}").render(**data) == "3"


def test_dict_attr_missing_key_is_loud():
    from jinja2 import UndefinedError
    with pytest.raises(UndefinedError):
        _SANDBOX_ENV.from_string("{{ d.nope }}").render(d={"a": 1})


def test_compile_expression_keeps_item_first_semantics():
    expr = _SANDBOX_ENV.compile_expression(
        "steps.fetch.result.items", undefined_to_none=False)
    assert expr(steps={"fetch": {"result": {"items": [1, 2]}}}) == [1, 2]


# ---------------------------------------------------------------- phase 2

def test_regex_filters():
    out = _SANDBOX_ENV.from_string(
        r"{{ raw | regex_replace('[\s\-()]+') | regex_replace('^\+972', '0') }}"
    ).render(raw="+972 52-123(45)67")
    assert out == "0521234567"
    assert _SANDBOX_ENV.from_string(
        r"{{ 'order #4521 shipped' | regex_search('#(\d+)', 1) }}").render() == "4521"
    assert _SANDBOX_ENV.from_string(
        r"{{ 'a1 b22 c333' | regex_findall('\d+') | length }}").render() == "3"
    assert _SANDBOX_ENV.from_string(
        "{{ 'a,b,c' | split(',') | length }}").render() == "3"


# ---------------------------------------------------------------- phase 3

def test_value_assignment_compiles_to_state_set():
    pb = compile_playbook(
        "playbook(name='t', description='d')\n"
        "n = inputs.count + 1\n"
    )
    step = pb.steps[0].model_dump(exclude_none=True)
    assert step["id"] == "n"
    assert step["kind"] == "state"
    assert step["state"] == [
        {"op": "set", "var": "n", "value": "{{ inputs.count + 1 }}"}]


def test_value_assignment_bare_name_resolves_to_vars():
    pb = compile_playbook(
        "playbook(name='t', description='d')\n"
        "n = inputs.count + 1\n"
        "gate = if_(n > 3, then=[tool('send_chat_message', message='hi')])\n"
    )
    assert pb.steps[1].when == "{{ vars.n > 3 }}"


def test_value_assignment_fstring_and_jinja_string():
    pb = compile_playbook(
        "playbook(name='t', description='d')\n"
        "n = 2\n"
        'greeting = f"hello {n}"\n'
        "phone = '{{ inputs.raw | regex_replace(\"[^0-9]\") }}'\n"
    )
    dumped = [s.model_dump(exclude_none=True) for s in pb.steps]
    assert dumped[1]["state"][0]["value"] == "hello {{ vars.n }}"
    assert dumped[2]["state"][0]["value"] == '{{ inputs.raw | regex_replace("[^0-9]") }}'


def test_value_assignment_walrus_in_nested_body():
    pb = compile_playbook(
        "playbook(name='t', description='d')\n"
        "scan = loop(over=[1, 2], item_name='x', body=[\n"
        "    (doubled := x * 2),\n"
        "    tool('send_chat_message', message='{{ vars.doubled }}'),\n"
        "])\n"
    )
    body = [s.model_dump(exclude_none=True) for s in pb.steps[0].body]
    assert body[0]["kind"] == "state"
    assert body[0]["state"] == [
        {"op": "set", "var": "doubled", "value": "{{ x * 2 }}"}]


def test_typoed_combinator_still_errors():
    with pytest.raises(PlaybookCompileError) as e:
        compile_playbook(
            "playbook(name='t', description='d')\n"
            "x = tol('web_fetch')\n"
        )
    assert "Unknown step function 'tol'" in e.value.issues[0].message


def test_value_assignment_duplicate_name_rejected():
    with pytest.raises(PlaybookCompileError) as e:
        compile_playbook(
            "playbook(name='t', description='d')\n"
            "x = 1\n"
            "x = tool('web_fetch', url='u')\n"
        )
    assert "Duplicate step id 'x'" in e.value.issues[0].message


# ---------------------------------------------------------------- phase 4

class _Bus:
    async def emit(self, name: str, payload: dict) -> None:
        pass


class _Runner:
    _tools = None


@pytest.fixture
async def tools():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield {td.name: h for td, h in build_tools(sf, _Bus(), _Runner())}
    await engine.dispose()


@pytest.mark.asyncio
async def test_language_reference_tool(tools):
    out = json.loads(await tools["playbook_language_reference"]())
    assert out["language_reference"] == LANGUAGE_CHEATSHEET


@pytest.mark.asyncio
async def test_edit_read_stage_carries_reference(tools):
    await tools["playbook_propose"](
        name="greeter",
        code="playbook(name='greeter', description='d')\n"
             "say = tool('send_chat_message', message=inputs.greeting)\n",
    )
    out = parse_read_stage(await tools["playbook_edit"](name="greeter"))
    assert out["stage"] == "read"
    assert out["language_reference"] == LANGUAGE_CHEATSHEET


@pytest.mark.asyncio
async def test_dry_run_end_to_end_assignment_vars_and_paths():
    """The whole chain: `x = expr` state step runs, bare-name refs resolve as
    vars.x, regex filters render, and stubbed tool output is read item-first
    through `.result.items` (previously the dict-method footgun)."""
    from plugin_playbooks.runner import PlaybookRunner

    class _Tool:
        def __init__(self, handler):
            self.handler = handler

    class _Tools:
        def __init__(self, **tools):
            self._tools = tools

        def get(self, name):
            return self._tools[name]

        def names(self):
            return list(self._tools)

    async def _noop(**_kw):
        return {"ok": True}

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(fetch_rows=_Tool(_noop), send_chat_message=_Tool(_noop)),
        events=_Bus(),
    )
    pb_def = compile_playbook(
        "playbook(name='e2e', description='d')\n"
        "phone = '{{ inputs.raw | regex_replace(\"[^0-9+]\") | regex_replace(\"^\\\\+972\", \"0\") }}'\n"
        "rows = tool('fetch_rows')\n"
        "gate = if_('{{ steps.rows.result.items | length > 0 }}', then=[\n"
        "    tool('send_chat_message', message='{{ vars.phone }}'),\n"
        "])\n"
    )
    from plugin_playbooks.models import Playbook
    pb = Playbook(
        name="e2e", display_name="e2e", status="enabled",
        definition=pb_def.model_dump(mode="json", exclude_none=True, by_alias=True),
    )
    out = await runner.dry_run(
        pb,
        inputs={"raw": "+972 52-123(45)67"},
        stubs={"rows": {"items": [1, 2]}},
    )
    trace = {t["step_id"]: t for t in out["trace"]}
    assert trace["phone"]["output"]["ops"][0]["after"] == "0521234567"
    assert trace["gate"]["output"] == {"branch": "then", "condition": True}
    assert trace["send_chat_message"]["resolved_inputs"]["message"] == "0521234567"
    await engine.dispose()


@pytest.mark.asyncio
async def test_validate_attaches_reference_only_on_error(tools):
    bad = json.loads(await tools["playbook_validate"](
        code="playbook(name='t', description='d')\nx = tol('web_fetch')\n"))
    assert not bad["ok"]
    assert bad["language_reference"] == LANGUAGE_CHEATSHEET
    good = json.loads(await tools["playbook_validate"](
        code="playbook(name='t', description='d')\n"
             "say = tool('send_chat_message', message='hi')\n"))
    assert good["ok"]
    assert "language_reference" not in good
