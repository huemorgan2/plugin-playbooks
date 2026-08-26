"""pblang — compiler + codegen tests (phase 1 of plans/002).

The compiler AST-parses restricted Python (never executes it) and emits the
PlaybookDef IR; codegen renders the IR back with variable names == step ids.
The load-bearing property is the round-trip: compile(codegen(ir)) == ir.
"""

from __future__ import annotations

import pytest

from plugin_playbooks.definition import PlaybookDef, StepKind, parse_yaml
from plugin_playbooks.pblang import (
    PlaybookCompileError,
    compile_playbook,
    defs_equal,
    generate_code,
)


def _dump(pb: PlaybookDef) -> dict:
    return pb.model_dump(mode="json", exclude_none=True, by_alias=True)


# ---------------------------------------------------------------- compiling


def test_tool_step_with_loose_kwargs_and_expression():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "fetch = tool('web_fetch', url=inputs.url, limit=5)\n"
        "again = tool('web_fetch', url=fetch.result.next_url)\n"
    )
    s0, s1 = pb.steps
    assert s0.kind == StepKind.TOOL_CALL
    assert s0.id == "fetch"
    assert s0.args == {"url": "{{ inputs.url }}", "limit": 5}
    # a previously assigned step name resolves to steps.<id>
    assert s1.args == {"url": "{{ steps.fetch.result.next_url }}"}


def test_string_args_pass_through_verbatim():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "x = tool('web_fetch', url='{{ vars.cur }}')\n"
    )
    assert pb.steps[0].args == {"url": "{{ vars.cur }}"}


def test_llm_fstring_prompt_and_output_schema():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "fetch = tool('gmail__fetch', query='x')\n"
        "cls = llm(f'Classify: {fetch.result.body}',\n"
        "          output={'label': 'str'}, purpose='reasoning')\n"
    )
    s = pb.steps[1]
    assert s.kind == StepKind.LLM_STEP
    assert s.prompt == "Classify: {{ steps.fetch.result.body }}"
    assert s.output_schema == {"label": "str"}
    assert s.purpose == "reasoning"


def test_agent_step_with_tools():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "a = agent('Decide.', output={'ok': 'bool'}, tools=['send_chat_message'])\n"
    )
    s = pb.steps[0]
    assert s.kind == StepKind.AGENT_STEP
    assert s.tools == ["send_chat_message"]


def test_condition_with_python_expression():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "check = llm('Check', output={'n': 'number'})\n"
        "gate = if_(check.n > 3 and inputs.strict,\n"
        "           then=[tool('send_chat_message', message='hi', id='say')],\n"
        "           else_=[halt(id='stop')])\n"
    )
    gate = pb.steps[1]
    assert gate.kind == StepKind.CONDITION
    assert gate.when == "{{ steps.check.n > 3 and inputs.strict }}"
    assert gate.then[0].id == "say"
    assert gate.else_[0].kind == StepKind.HALT


def test_loop_item_scope_and_collect():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "fetch = tool('gmail__fetch', query='x')\n"
        "scan = loop(\n"
        "    over=fetch.result.messages,\n"
        "    item_name='email',\n"
        "    body=[llm(f'Is {email} spam? ({email_index})',\n"
        "              output={'spam': 'bool'}, id='cls')],\n"
        "    collect=cls.spam,\n"
        ")\n"
    )
    scan = pb.steps[1]
    assert scan.over == "{{ steps.fetch.result.messages }}"
    assert scan.body[0].prompt == "Is {{ email }} spam? ({{ email_index }})"
    assert scan.collect == "{{ steps.cls.spam }}"


def test_loop_item_name_out_of_scope_after_body():
    with pytest.raises(PlaybookCompileError) as ei:
        compile_playbook(
            "playbook(name='t')\n"
            "scan = loop(over=[1], item_name='n', body=[state(set_('a', 1))])\n"
            "after = llm(f'{n}')\n"
        )
    assert any("Unknown name 'n'" in i.message for i in ei.value.issues)


def test_walrus_ids_inside_nested_lists():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "l = loop(over=[1, 2], item_name='n',\n"
        "         body=[(y := llm(f'double {n}', output={'v': 'number'}))],\n"
        "         collect=y.v)\n"
    )
    assert pb.steps[0].body[0].id == "y"
    assert pb.steps[0].collect == "{{ steps.y.v }}"


def test_generated_ids_are_deduped():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "tool('web_fetch', url='a')\n"
        "tool('web_fetch', url='b')\n"
    )
    assert [s.id for s in pb.steps] == ["web_fetch", "web_fetch_2"]


def test_parallel_branches():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "p = parallel([\n"
        "    [tool('a_tool', id='left')],\n"
        "    [tool('b_tool', id='right')],\n"
        "])\n"
    )
    p = pb.steps[0]
    assert p.kind == StepKind.PARALLEL
    assert p.branches[0][0].id == "left"
    assert p.branches[1][0].id == "right"


def test_state_ops_and_halt():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "seed = state(set_('frontier', '[ inputs.start ]'), set_('seen', []))\n"
        "take = state(pop_front('frontier', into='cur'),\n"
        "             add_unique('seen', '{{ vars.cur }}'), incr('count'))\n"
        "stop = halt(when='{{ vars.count > 5 }}', value='{{ vars.seen }}')\n"
    )
    seed, take, stop = pb.steps
    assert [o.op for o in seed.state] == ["set", "set"]
    assert seed.state[1].value == []
    assert take.state[0].into == "cur"
    assert take.state[2].op == "incr"
    assert stop.when == "{{ vars.count > 5 }}"


def test_wait_event_approve_subtask():
    pb = compile_playbook(
        "playbook(name='t')\n"
        "ok = approve(show=['{{ steps.x.report }}'])\n"
        "ev = wait_event('email.received', filter={'label': 'x'},\n"
        "                timeout_seconds=60)\n"
        "sub = subtask('other-playbook', inputs={'q': inputs.q},\n"
        "              returns={'out': '{{ steps.final.v }}'})\n"
    )
    ok, ev, sub = pb.steps
    assert ok.kind == StepKind.WAIT_FOR_APPROVAL
    assert ev.event == "email.received"
    assert ev.event_filter == {"label": "x"}
    assert sub.playbook == "other-playbook"
    assert sub.inputs_map == {"q": "{{ inputs.q }}"}


def test_header_triggers_and_options():
    pb = compile_playbook(
        "playbook(\n"
        "    name='mail-watch',\n"
        "    description='watch mail',\n"
        "    agent_autonomy='agent_may_trigger',\n"
        "    inputs={'type': 'object', 'properties': {'q': {'type': 'string'}}},\n"
        "    triggers=[trigger(event='email.received',\n"
        "                      map={'q': '{{ event.payload }}'})],\n"
        ")\n"
        "x = tool('a_tool', retry=2, on_error='continue', timeout=30,\n"
        "         explanation='does a thing')\n"
    )
    assert pb.name == "mail-watch"
    assert pb.agent_autonomy.value == "agent_may_trigger"
    assert pb.triggers[0].event == "email.received"
    s = pb.steps[0]
    assert s.retry.max == 2
    assert s.on_error.value == "continue"
    assert s.timeout == 30
    assert s.explanation == "does a thing"


def test_name_param_overrides_declared_name():
    pb = compile_playbook("playbook(name='x')\n", name="forced")
    assert pb.name == "forced"


# ------------------------------------------------------------------- errors


def _errors_of(source: str) -> list[str]:
    with pytest.raises(PlaybookCompileError) as ei:
        compile_playbook(source)
    return [i.message for i in ei.value.issues]


def test_python_control_flow_is_rejected_with_hints():
    msgs = _errors_of(
        "playbook(name='t')\n"
        "for x in range(3):\n"
        "    pass\n"
    )
    assert any("for/while loops are not allowed" in m for m in msgs)


def test_imports_and_defs_rejected():
    msgs = _errors_of("playbook(name='t')\nimport os\ndef f(): pass\n")
    assert any("Imports are not allowed" in m for m in msgs)
    assert any("definitions are not allowed" in m for m in msgs)


def test_unknown_name_and_call_in_expression():
    msgs = _errors_of(
        "playbook(name='t')\n"
        "x = tool('a_tool', v=mystery.field)\n"
        "y = tool('a_tool', v=len(inputs.items))\n"
    )
    assert any("Unknown name 'mystery'" in m for m in msgs)
    assert any("Function calls are not allowed" in m for m in msgs)


def test_missing_playbook_declaration():
    msgs = _errors_of("x = tool('a_tool')\n")
    assert any("playbook(...) declaration" in m for m in msgs)


def test_duplicate_ids_and_id_mismatch():
    msgs = _errors_of(
        "playbook(name='t')\n"
        "x = tool('a_tool')\n"
        "x = tool('b_tool')\n"
        "y = tool('c_tool', id='z')\n"
    )
    assert any("Duplicate step id 'x'" in m for m in msgs)
    assert any("assigned to 'y'" in m for m in msgs)


def test_all_errors_reported_at_once_with_line_numbers():
    with pytest.raises(PlaybookCompileError) as ei:
        compile_playbook(
            "playbook(name='t')\n"
            "import os\n"
            "x = tool('a_tool', v=nope)\n"
        )
    lines = sorted(i.line for i in ei.value.issues)
    assert lines == [2, 3]


def test_syntax_error_is_a_compile_error():
    with pytest.raises(PlaybookCompileError):
        compile_playbook("playbook(name='t'\n")


# --------------------------------------------------------------- round-trip


CRAWL_YAML = """
name: site-crawl
description: BFS crawl
inputs:
  type: object
  properties:
    start_url: {type: string}
steps:
  - id: seed
    kind: state
    state:
      - {op: set, var: frontier, value: '[ inputs.start_url ]'}
      - {op: set, var: visited, value: '[]'}
  - id: crawl
    kind: loop
    while: '{{ vars.frontier | length > 0 }}'
    max_iterations: 200
    body:
      - id: take
        kind: state
        state:
          - {op: pop_front, var: frontier, into: cur}
          - {op: add_unique, var: visited, value: '{{ vars.cur }}'}
      - id: fetch
        kind: tool_call
        tool: web_fetch
        args: {url: '{{ vars.cur }}'}
      - id: links
        kind: llm_step
        output_schema: {links: array}
        prompt: "List internal link URLs on this page:\\n{{ steps.fetch.result }}"
      - id: enqueue
        kind: loop
        over: '{{ steps.links.links }}'
        item_name: link
        body:
          - id: gate
            kind: condition
            when: '{{ link not in vars.visited and link not in vars.frontier }}'
            then:
              - id: push
                kind: state
                state: [{op: push_back, var: frontier, value: '{{ link }}'}]
"""

SUBSCRIPTIONS_YAML = """
name: subscription-scan
display_name: Subscription scan
description: Scan emails for paid subscriptions
when_to_use: When the owner asks what subscriptions they pay for
triggers:
  - event: email.received
    filter: {label: receipts}
    map: {email: '{{ event.payload }}'}
steps:
  - id: fetch
    kind: tool_call
    tool: gmail__gmail__fetch_emails
    args: {query: 'after:2024/12/15 (receipt OR invoice)'}
  - id: scan
    kind: loop
    over: '{{ steps.fetch.result.messages }}'
    item_name: email
    concurrency: 4
    collect: '{{ steps.classify }}'
    body:
      - id: classify
        kind: llm_step
        output_schema: {is_subscription: bool, service: str, amount: number}
        prompt: 'Is THIS ONE email a paid subscription? {{ email }}'
  - id: report
    kind: llm_step
    output_schema: {report: str}
    prompt: "Build a report from:\\n{{ steps.scan.collected | selectattr('is_subscription') | list }}"
    retry: {max: 2, backoff_seconds: 5.0}
  - id: gate
    kind: wait_for_approval
    show: ['{{ steps.report.report }}']
  - id: notify
    kind: tool_call
    tool: send_chat_message
    args: {message: '{{ steps.report.report }}'}
    on_error: continue
"""

PARALLEL_SUBTASK_YAML = """
name: fanout
steps:
  - id: fan
    kind: parallel
    fan_in: all
    branches:
      - - id: left
          kind: tool_call
          tool: a_tool
          args: {}
      - - id: right
          kind: subtask
          playbook: other
          inputs_map: {q: '{{ inputs.q }}'}
          returns: {v: '{{ steps.x.v }}'}
  - id: waitmail
    kind: wait_for_event
    event: email.received
    event_filter: {label: x}
    timeout_seconds: 120
  - id: maybe_stop
    kind: halt
    when: '{{ steps.waitmail.timed_out }}'
    value: nothing to do
"""


@pytest.mark.parametrize("yaml_src", [
    CRAWL_YAML, SUBSCRIPTIONS_YAML, PARALLEL_SUBTASK_YAML,
], ids=["crawl", "subscriptions", "parallel-subtask"])
def test_roundtrip_from_yaml_fixtures(yaml_src):
    original = parse_yaml(yaml_src)
    code = generate_code(original)
    recompiled = compile_playbook(code)
    assert defs_equal(original, recompiled), (
        f"round-trip drift.\n--- code ---\n{code}\n"
        f"--- original ---\n{_dump(original)}\n"
        f"--- recompiled ---\n{_dump(recompiled)}"
    )


def test_roundtrip_preserves_step_ids_exactly():
    original = parse_yaml(CRAWL_YAML)
    code = generate_code(original)
    # ids become variable names / id= kwargs verbatim
    for sid in ("seed", "crawl", "take", "fetch", "links", "enqueue",
                "gate", "push"):
        assert sid in code
    recompiled = compile_playbook(code)

    def ids(steps):
        out = []
        for s in steps:
            out.append(s.id)
            for sub in (s.then, s.else_, s.body):
                if sub:
                    out.extend(ids(sub))
            if s.branches:
                for b in s.branches:
                    out.extend(ids(b))
        return out

    assert ids(recompiled.steps) == ids(original.steps)


def test_roundtrip_reserved_or_invalid_ids_use_id_kwarg():
    original = PlaybookDef.model_validate({
        "name": "odd-ids",
        "steps": [
            {"id": "state", "kind": "tool_call", "tool": "a_tool", "args": {}},
            {"id": "my-step", "kind": "tool_call", "tool": "b_tool", "args": {}},
        ],
    })
    code = generate_code(original)
    assert 'id="state"' in code.replace("'", '"')
    assert 'id="my-step"' in code.replace("'", '"')
    assert defs_equal(original, compile_playbook(code))


def test_roundtrip_multiline_prompt():
    original = PlaybookDef.model_validate({
        "name": "ml",
        "steps": [{
            "id": "x", "kind": "llm_step",
            "prompt": "Line one\nLine two: {{ inputs.q }}\n",
            "output_schema": {"v": "str"},
        }],
    })
    assert defs_equal(original, compile_playbook(generate_code(original)))


def test_roundtrip_code_authored_playbook():
    src = (
        "playbook(name='t', description='d')\n"
        "fetch = tool('web_fetch', url=inputs.url)\n"
        "scan = loop(over=fetch.result.items, item_name='it',\n"
        "            body=[llm(f'judge {it}', output={'ok': 'bool'}, id='j')],\n"
        "            collect=j.ok)\n"
    )
    pb = compile_playbook(src)
    assert defs_equal(pb, compile_playbook(generate_code(pb)))


def test_skill_examples_compile():
    """Every ```python block in the authoring skill must compile and round-trip."""
    import re

    from plugin_playbooks import _AUTHORING_SKILL_BODY

    blocks = re.findall(r"```python\n(.*?)```", _AUTHORING_SKILL_BODY, re.S)
    assert len(blocks) >= 2
    for block in blocks:
        pb = compile_playbook(block)
        assert defs_equal(pb, compile_playbook(generate_code(pb)))
