"""plans/032 phase 01 — the v2 static checker and the format sniff.

Pure functions: `check`, `sniff_format`, `resolve_format`, `stable_filename`.
No DB, no fixtures beyond the conftest luna_sdk stub. Every rule in `RULES`
has a snippet in `CORPUS` below (the `test_every_issue_has_example_fix`
parametrization proves no rule is untested).
"""

from __future__ import annotations

import textwrap

import pytest

from plugin_playbooks.v2 import (
    APPROVE_RESULT_KEYS,
    AVAILABLE_EFFECTS,
    CTX_EXCEPTIONS,
    CTX_UNCATCHABLE,
    DEFAULT_FEATURES,
    MAX_EFFECTS,
    UNAVAILABLE_EFFECTS,
)
from plugin_playbooks.v2.checker import (
    RULES,
    CheckIssue,
    check,
    resolve_format,
    sniff_format,
    stable_filename,
)

# The master plan §2 "Language" example, verbatim (docs/v2.md §1).
EXAMPLE = '''async def run(ctx, inputs):
    rows = await ctx.tool("fetch_list", url=inputs["url"])
    good = [r for r in rows["items"] if r["score"] > 3]
    summaries = []
    for r in good:
        s = await ctx.llm(f"Summarize {r['title']}", output={"s": "str"})
        summaries.append(s["s"])
    await ctx.approve(show=summaries)
    await ctx.tool("send_message", to=inputs["owner"], text="\\n".join(summaries))
    return {"count": len(summaries)}
'''

# docs/v2.md §1 second example — the dojop/01 grader constructs
# (`while`, `.pop(0)`, `try:`/`except ctx.ToolError`, `raise`, `ctx.gather`).
QUEUE_EXAMPLE = '''async def run(ctx, inputs):
    queue = list(inputs["urls"])
    pages = []
    failed = []
    while queue:
        url = queue.pop(0)
        try:
            page = await ctx.tool("fetch_page", url=url, _retry=2)
        except ctx.ToolError as e:
            failed.append({"url": url, "error": str(e)})
            continue
        pages.append(page)
        for link in page.get("links", []):
            if link not in queue and link not in [p["url"] for p in pages]:
                queue.append(link)
    if not pages:
        raise ValueError("nothing fetched: " + ", ".join(f["url"] for f in failed))
    summaries = await ctx.gather(*[
        ctx.llm(f"Summarize {p['text']}", output={"s": "str"}, _id="summary")
        for p in pages
    ])
    report = "\\n".join(s["s"] for s in summaries)
    await ctx.tool("send_message", to=inputs["owner"], text=report)
    return {"pages": len(pages), "failed": failed}
'''


def _pb(body: str) -> str:
    """Wrap an indented body into a minimal playbook."""
    return "async def run(ctx, inputs):\n" + textwrap.indent(textwrap.dedent(body), "    ")


def _codes(result) -> list[str]:
    return [i.code for i in result.issues]


def _one(result, code: str) -> CheckIssue:
    hits = [i for i in result.issues if i.code == code]
    assert hits, f"{code} not reported; got {_codes(result)}"
    return hits[0]


def _assert_shape(code_text: str, issue: CheckIssue, rule: str) -> None:
    assert issue.code == rule
    assert issue.severity == RULES[rule].severity
    assert issue.expected and issue.example_fix and issue.message
    assert issue.line >= 1
    assert issue.source_line == code_text.splitlines()[issue.line - 1]
    d = issue.to_dict()
    assert set(d) == {
        "line", "col", "source_line", "message", "expected", "example_fix", "code", "severity",
    }


# --------------------------------------------------------------------------- clean
def test_master_example_passes_clean():
    r = check(EXAMPLE, name="t", version=1)
    assert r.issues == []
    assert r.ok
    r2 = check(EXAMPLE, name="t", version=1, tool_names={"fetch_list", "send_message"})
    assert r2.issues == []
    assert r2.summary["tools"] == ["fetch_list", "send_message"]
    assert [c["id"] for c in r2.summary["call_sites"]] == ["rows", "s", "approve", "send_message"]
    assert r2.summary["format"] == "python"
    assert r2.summary["inputs_read"] == ["owner", "url"]
    assert r2.summary["imports"] == []
    assert r2.summary["subtasks"] == []


def test_queue_example_passes_clean():
    r = check(QUEUE_EXAMPLE, name="crawl", version=2)
    assert r.issues == []
    assert r.ok
    ids = [c["id"] for c in r.summary["call_sites"]]
    assert ids == ["page", "summary", "send_message"]
    page = r.summary["call_sites"][0]
    assert page["loop_depth"] == 1 and page["in_try"] is True
    assert r.summary["tools"] == ["fetch_page", "send_message"]


# --------------------------------------------------------------------------- corpus
# (rule code, source, extra kwargs for check())
CORPUS: dict[str, tuple[str, dict]] = {
    "v2-syntax": ("async def run(ctx, inputs):\n    x = (1,\n", {}),
    "v2-entry-point": ("def run(ctx, inputs):\n    return 1\n", {}),
    "v2-top-level": ("x = compute()\nasync def run(ctx, inputs):\n    return x\n", {}),
    "v2-import": ("import os\nasync def run(ctx, inputs):\n    return os.getcwd()\n", {}),
    "v2-banned-name": (_pb('return open("x").read()'), {}),
    "v2-use-ctx-now": (
        "from datetime import datetime\nasync def run(ctx, inputs):\n    return datetime.now()\n",
        {},
    ),
    "v2-use-ctx-random": ("import random\nasync def run(ctx, inputs):\n    return random.random()\n", {}),
    "v2-use-ctx-log": (_pb('print("hi")\nreturn 1'), {}),
    "v2-use-ctx-gather": (
        "import asyncio\nasync def run(ctx, inputs):\n"
        '    return await asyncio.gather(ctx.tool("a"), ctx.tool("b"))\n',
        {},
    ),
    "v2-effect-unavailable": (_pb("await ctx.sleep(1)\nreturn 1"), {}),
    "v2-unknown-ctx-attr": (_pb('return await ctx.tools("x")'), {}),
    "v2-effect-not-on-ctx": (_pb('return await tool("x")'), {}),
    "v2-missing-await": (_pb('rows = ctx.tool("fetch")\nreturn rows'), {}),
    "v2-tool-name-literal": (_pb('name = "fetch"\nreturn await ctx.tool(name)'), {}),
    "v2-unknown-kwarg": (_pb('return await ctx.llm("p", temperature=0)'), {}),
    "v2-option": (_pb('return await ctx.tool("x", _cache=True)'), {}),
    "v2-duplicate-call-site-id": (
        _pb('a = await ctx.tool("x", _id="s")\nb = await ctx.tool("y", _id="s")\nreturn [a, b]'),
        {},
    ),
    "v2-unknown-tool": (_pb('return await ctx.tool("nope")'), {"tool_names": {"fetch"}}),
    "v2-broad-except": (
        _pb('try:\n    rows = await ctx.tool("fetch")\nexcept Exception:\n    rows = []\nreturn rows'),
        {},
    ),
    "v2-uncatchable": (
        _pb('try:\n    await ctx.tool("fetch")\nexcept ctx.RunCancelled:\n    pass\nreturn 1'),
        {},
    ),
    "v2-inputs-attr": (_pb("return inputs.url"), {}),
    "v2-unknown-input": (
        _pb('return inputs["nope"]'),
        {"inputs_schema": {"type": "object", "properties": {"url": {"type": "string"}}}},
    ),
    "v2-set-iteration": (_pb('out = []\nfor x in {"a", "b"}:\n    out.append(x)\nreturn out'), {}),
    "v2-nondeterministic-builtin": (_pb('return hash(inputs["url"])'), {}),
    "v2-too-many-call-sites": (
        _pb("\n".join(f'await ctx.tool("t{i}")' for i in range(41)) + "\nreturn 1"),
        {},
    ),
    "v2-effect-in-nested-loops": (
        _pb('for a in inputs["a"]:\n    for b in a:\n        await ctx.tool("t", b=b)\nreturn 1'),
        {},
    ),
    "v2-gather-args": (_pb('return await ctx.gather(ctx.tool("a"), 42)'), {}),
    "v2-async-construct": (_pb('async for x in inputs["xs"]:\n    pass\nreturn 1'), {}),
    "monolithic-playbook": (
        _pb('return await ctx.llm("Read all emails, summarize each one and then send a digest")'),
        {},
    ),
    "compound-leaf": (
        _pb(
            'rows = await ctx.tool("fetch")\n'
            'return await ctx.llm("Go through the rows one by one and then summarize")'
        ),
        {},
    ),
    "agent-does-work": (
        _pb(
            'rows = await ctx.tool("fetch")\n'
            'return await ctx.agent("Search the web and download the report")'
        ),
        {},
    ),
    "context-economy": (
        _pb(
            'rows = await ctx.tool("fetch")\n'
            'return await ctx.llm(f"Summarize {rows[\'items\']}")'
        ),
        {},
    ),
    # from resolve_format, not check()
    "v2-format-mismatch": ("async def run(ctx, inputs):\n    return 1\n", {"explicit": "pblang"}),
    "v2-format-unknown": ("async def run(ctx, inputs):\n    return 1\n", {"explicit": "yaml"}),
}

_FORMAT_RULES = {"v2-format-mismatch", "v2-format-unknown"}


def _run_corpus(rule: str):
    src, kw = CORPUS[rule]
    if rule in _FORMAT_RULES:
        _fmt, issue = resolve_format(kw["explicit"], src)
        assert issue is not None
        return src, [issue]
    return src, check(src, name="t", version=1, **kw).issues


@pytest.mark.parametrize("rule", sorted(RULES))
def test_rule(rule):
    src, issues = _run_corpus(rule)
    hits = [i for i in issues if i.code == rule]
    assert hits, f"{rule} not reported; got {[i.code for i in issues]}"
    issue = hits[0]
    if rule in _FORMAT_RULES:
        assert (issue.line, issue.col) == (1, 0)
        assert issue.severity == RULES[rule].severity
        assert issue.expected and issue.example_fix
    else:
        _assert_shape(src, issue, rule)


def test_every_issue_has_example_fix():
    seen: set[str] = set()
    for rule in RULES:
        _src, issues = _run_corpus(rule)
        assert all(i.example_fix and i.expected for i in issues), rule
        seen |= {i.code for i in issues}
    assert seen == set(RULES)


# --------------------------------------------------------------------------- fixed points
def test_rule_v2_syntax_position_and_only_issue():
    src = CORPUS["v2-syntax"][0]
    r = check(src, name="t", version=1)
    assert _codes(r) == ["v2-syntax"]
    i = r.issues[0]
    assert (i.line, i.col) == (2, 8)
    assert not r.ok


def test_rule_v2_entry_point_variants():
    for src in (
        "def run(ctx, inputs):\n    return 1\n",
        "async def go(ctx, inputs):\n    return 1\n",
        "async def run(ctx):\n    return 1\n",
        "async def run(ctx, inputs, extra=None):\n    return 1\n",
        "async def run(ctx, inputs, *args):\n    return 1\n",
        "async def run(c, i):\n    return 1\n",
        "async def run(ctx, inputs):\n    yield 1\n",
        "async def run(ctx, inputs):\n    return 1\nasync def run(ctx, inputs):\n    return 2\n",
    ):
        assert "v2-entry-point" in _codes(check(src, name="t", version=1)), src


def test_rule_v2_top_level_allows_constants_helpers_and_docstring():
    src = (
        '"""doc"""\n'
        "import json\n"
        "LIMIT = 3\n"
        "NAMES = [\"a\", \"b\"]\n"
        "def helper(x):\n    return x * 2\n"
        "async def run(ctx, inputs):\n    return helper(LIMIT)\n"
    )
    assert check(src, name="t", version=1).issues == []
    for bad in ("x = helper()\n", "if True:\n    pass\n", "class A:\n    pass\n", "for i in []:\n    pass\n"):
        r = check(bad + "async def run(ctx, inputs):\n    return 1\n", name="t", version=1)
        assert "v2-top-level" in _codes(r), bad


def test_rule_v2_import_steers():
    for mod, hint in (
        ("time", "ctx.now"), ("random", "ctx.random"), ("uuid", "ctx.random"),
        ("asyncio", "ctx.gather"), ("os", "ctx.tool"), ("requests", "ctx.tool"),
    ):
        r = check(f"import {mod}\nasync def run(ctx, inputs):\n    return 1\n", name="t", version=1)
        i = _one(r, "v2-import")
        assert hint in i.message + i.expected + i.example_fix, mod
    r = check("import asyncio\nasync def run(ctx, inputs):\n    return 1\n", name="t", version=1)
    assert "ctx.sleep is not available" in _one(r, "v2-import").message
    r = check("from x import y\nasync def run(ctx, inputs):\n    return 1\n", name="t", version=1)
    assert "v2-import" in _codes(r)
    r = check("import collections\nasync def run(ctx, inputs):\n    return 1\n", name="t", version=1)
    assert r.issues == [] and r.summary["imports"] == ["collections"]


def test_rule_v2_banned_name_attribute_forms():
    r = check(_pb("return inputs.__class__"), name="t", version=1)
    assert "v2-banned-name" in _codes(r)
    r = check(_pb('return eval("1")'), name="t", version=1)
    assert "v2-banned-name" in _codes(r)


def test_rule_v2_use_ctx_now_forms():
    for src in (
        "import datetime\nasync def run(ctx, inputs):\n    return datetime.datetime.utcnow()\n",
        "from datetime import date\nasync def run(ctx, inputs):\n    return date.today()\n",
        "import time\nasync def run(ctx, inputs):\n    return time.time()\n",
    ):
        assert "v2-use-ctx-now" in _codes(check(src, name="t", version=1)), src


def test_rule_v2_effect_unavailable_message_and_feature_flag():
    for src in (
        _pb("await ctx.sleep(1)\nreturn 1"),
        "import asyncio\nasync def run(ctx, inputs):\n    await asyncio.sleep(1)\n    return 1\n",
        "import time\nasync def run(ctx, inputs):\n    time.sleep(1)\n    return 1\n",
    ):
        i = _one(check(src, name="t", version=1), "v2-effect-unavailable")
        assert "not available in this version" in i.message, src
    # phase 07: wait_event is available by default (DEFAULT_FEATURES carries the flag)
    src = _pb('return await ctx.wait_event("x", timeout=5)')
    r = check(src, name="t", version=1)
    assert r.issues == []
    assert r.summary["call_sites"][0]["kind"] == "wait_event"
    # ... and R10 keeps its feature gate: an empty feature set still rejects it
    i = _one(check(src, name="t", version=1, features=frozenset()), "v2-effect-unavailable")
    assert "not available in this version" in i.message
    r = check(src, name="t", version=1, features={"wait_event"})
    assert r.issues == []
    r = check(_pb('return await ctx.wait_event("x")'), name="t", version=1, features={"wait_event"})
    assert "v2-effect-unavailable" not in _codes(r)
    i = _one(r, "v2-unknown-kwarg")
    assert "timeout" in i.message
    # ctx.sleep stays rejected even with every feature on
    r = check(_pb("await ctx.sleep(1)\nreturn 1"), name="t", version=1, features={"wait_event"})
    assert "v2-effect-unavailable" in _codes(r)


def test_wait_event_timeout_required_by_default():
    # phase 07: R15 fires on `ctx.wait_event("x")` with the default features
    r = check(_pb('return await ctx.wait_event("x")'), name="t", version=1)
    assert "v2-effect-unavailable" not in _codes(r)
    i = _one(r, "v2-unknown-kwarg")
    assert "timeout" in i.message
    assert "timeout" in (i.expected or "")


def test_rule_v2_unknown_ctx_attr_did_you_mean():
    i = _one(check(_pb('return await ctx.tools("x")'), name="t", version=1), "v2-unknown-ctx-attr")
    assert "ctx.tool" in i.message or "ctx.tool" in i.expected
    # exception attributes are known
    src = _pb('try:\n    return await ctx.tool("x")\nexcept ctx.EffectError:\n    return None')
    assert check(src, name="t", version=1).issues == []


def test_rule_v2_effect_not_on_ctx_forms():
    for src in (
        _pb('return await tool("x")'),
        _pb('return await llm("p")'),
        _pb("loop(over=inputs)\nreturn 1"),
        _pb('return await self.tool("x")'),
        'playbook(name="x")\nasync def run(ctx, inputs):\n    return 1\n',
    ):
        assert "v2-effect-not-on-ctx" in _codes(check(src, name="t", version=1)), src
    # a whitelisted module's method that shares a name is not an effect
    src = "import math\nasync def run(ctx, inputs):\n    return math.log(2)\n"
    assert check(src, name="t", version=1).issues == []


def test_rule_v2_missing_await_gather_forms():
    clean = (
        _pb('return await ctx.gather(ctx.tool("a"), ctx.tool("b"))'),
        _pb('return await ctx.gather(*[ctx.tool("a", i=i) for i in range(3)])'),
        _pb('cs = [ctx.tool("a", i=i) for i in range(3)]\nreturn await ctx.gather(*cs)'),
        _pb('cs = []\nfor i in range(3):\n    cs.append(ctx.tool("a", i=i))\nreturn await ctx.gather(*cs)'),
    )
    for src in clean:
        r = check(src, name="t", version=1)
        assert "v2-missing-await" not in _codes(r), src
    bad = (
        _pb('ctx.log("x")\nreturn 1'),
        _pb('cs = [ctx.tool("a", i=i) for i in range(3)]\nreturn cs'),
        _pb('cs = [ctx.tool("a", i=i) for i in range(3)]\nx = cs[0]\nreturn await ctx.gather(*cs)'),
        _pb('return ctx.gather(ctx.tool("a"))'),
    )
    for src in bad:
        assert "v2-missing-await" in _codes(check(src, name="t", version=1)), src


def test_rule_v2_tool_name_literal_forms():
    for src in (
        _pb("return await ctx.tool()"),
        _pb('return await ctx.tool("x", 1)'),
        _pb('return await ctx.subtask(inputs["pb"])'),
        _pb('return await ctx.tool(*inputs["args"])'),
    ):
        assert "v2-tool-name-literal" in _codes(check(src, name="t", version=1)), src


def test_rule_v2_unknown_kwarg_forms():
    r = check(_pb('return await ctx.llm("p", temperature=0)'), name="t", version=1)
    i = _one(r, "v2-unknown-kwarg")
    for name in ("output", "purpose", "model", "system"):
        assert name in i.expected
    for src in (
        _pb("return await ctx.approve()"),
        _pb("return await ctx.approve(1)"),
        _pb("return await ctx.now(1)"),
        _pb('return await ctx.log("a", "b")'),
        _pb('return await ctx.agent("p", tools=[], model="x")'),
        _pb('return await ctx.subtask("x", {}, ["k"])'),
        _pb('return await ctx.llm()'),
    ):
        assert "v2-unknown-kwarg" in _codes(check(src, name="t", version=1)), src
    # tool keywords are free-form; `**kw` is allowed on ctx.tool only
    assert check(_pb('return await ctx.tool("x", **inputs)'), name="t", version=1).issues == []
    assert "v2-unknown-kwarg" in _codes(check(_pb('return await ctx.llm("p", **inputs)'), name="t", version=1))


def test_rule_v2_option_forms():
    for src in (
        _pb('return await ctx.tool("x", _cache=True)'),
        _pb('return await ctx.tool("x", _id=inputs["id"])'),
        _pb('return await ctx.tool("x", _timeout="10")'),
        _pb('return await ctx.tool("x", _retry="3")'),
        _pb("return await ctx.approve(show=1, _timeout=3)"),
        _pb('return await ctx.subtask("x", _retry=2)'),
        _pb('return await ctx.gather(ctx.tool("a"), _id="g")'),
    ):
        assert "v2-option" in _codes(check(src, name="t", version=1)), src
    ok = _pb(
        'a = await ctx.tool("x", _id="a", _timeout=10, _retry={"attempts": 3, "backoff": 1.5})\n'
        'b = await ctx.llm("p", _id="b", _timeout=5.5, _retry=2)\n'
        'c = await ctx.subtask("child", {"k": 1}, returns=["x"], _id="c", _timeout=60)\n'
        'd = await ctx.approve(show=a, _id="d")\n'
        'return [a, b, c, d]'
    )
    assert check(ok, name="t", version=1).issues == []


def test_rule_v2_unknown_tool_only_with_tool_names():
    src = _pb('return await ctx.tool("nope")')
    assert check(src, name="t", version=1).issues == []
    r = check(src, name="t", version=1, tool_names={"fetch"})
    i = _one(r, "v2-unknown-tool")
    assert "fetch" in i.expected
    assert "v2-unknown-tool" not in _codes(check(src, name="t", version=1, tool_names={"nope"}))


def test_rule_v2_broad_except_forms():
    for handler in ("except:", "except Exception:", "except BaseException:", "except (ValueError, Exception):"):
        src = _pb(f'try:\n    rows = await ctx.tool("fetch")\n{handler}\n    rows = []\nreturn rows')
        i = _one(check(src, name="t", version=1), "v2-broad-except")
        assert "ctx.EffectError" in i.message or "ctx.EffectError" in i.expected
    # a broad except around pure compute is not flagged
    src = _pb('try:\n    x = inputs["a"] / inputs["b"]\nexcept Exception:\n    x = 0\nreturn x')
    assert "v2-broad-except" not in _codes(check(src, name="t", version=1))


def test_rule_v2_uncatchable_forms():
    for exc in sorted(CTX_UNCATCHABLE):
        src = _pb(f'try:\n    await ctx.tool("fetch")\nexcept ctx.{exc}:\n    pass\nreturn 1')
        assert "v2-uncatchable" in _codes(check(src, name="t", version=1)), exc
    for exc in sorted(CTX_EXCEPTIONS):
        src = _pb(f'try:\n    await ctx.tool("fetch")\nexcept ctx.{exc}:\n    pass\nreturn 1')
        assert check(src, name="t", version=1).issues == [], exc


def test_rule_v2_inputs_attr_and_namespaces():
    for src in (_pb("return inputs.url"), _pb("return steps.fetch"), _pb("return vars.x"), _pb("return event")):
        assert "v2-inputs-attr" in _codes(check(src, name="t", version=1)), src
    # dict methods are fine; a locally bound `event` is fine
    assert check(_pb('return inputs.get("url")'), name="t", version=1).issues == []
    assert check(_pb('event = await ctx.tool("x")\nreturn event'), name="t", version=1).issues == []


def test_rule_v2_unknown_input_only_with_schema():
    src = _pb('return [inputs["nope"], inputs.get("also"), inputs["url"]]')
    schema = {"type": "object", "properties": {"url": {"type": "string"}}}
    assert check(src, name="t", version=1).issues == []
    r = check(src, name="t", version=1, inputs_schema=schema)
    assert [i.code for i in r.issues] == ["v2-unknown-input", "v2-unknown-input"]
    assert r.summary["inputs_read"] == ["also", "nope", "url"]


def test_rule_v2_set_iteration_forms():
    for src in (
        _pb('return [x for x in {"a", "b"}]'),
        _pb('return ", ".join(set(inputs["xs"]))'),
        _pb('return list({x for x in inputs["xs"]})'),
        _pb('seen = set(inputs["xs"])\nout = []\nfor s in seen:\n    out.append(s)\nreturn out'),
    ):
        i = _one(check(src, name="t", version=1), "v2-set-iteration")
        assert "sorted(" in i.example_fix
    assert check(_pb('return sorted(set(inputs["xs"]))'), name="t", version=1).issues == []


def test_rule_v2_nondeterministic_builtin_forms():
    assert "v2-nondeterministic-builtin" in _codes(check(_pb("return id(inputs)"), name="t", version=1))


def test_rule_v2_effect_in_nested_loops_message_names_cap():
    src, _ = CORPUS["v2-effect-in-nested-loops"]
    i = _one(check(src, name="t", version=1), "v2-effect-in-nested-loops")
    assert f"MAX_EFFECTS = {MAX_EFFECTS}" in i.message
    one_deep = _pb('for a in inputs["a"]:\n    await ctx.tool("t", a=a)\nreturn 1')
    assert check(one_deep, name="t", version=1).issues == []


def test_rule_v2_gather_args_forms():
    for src in (
        _pb('return await ctx.gather(ctx.tool("a"), ctx.gather(ctx.tool("b")))'),
        _pb('return await ctx.gather(helper())'),
        _pb('return await ctx.gather(await ctx.tool("a"))'),
    ):
        assert "v2-gather-args" in _codes(check(src, name="t", version=1)), src


def test_rule_v2_async_construct_forms():
    assert "v2-async-construct" in _codes(check(_pb("async with inputs as x:\n    pass\nreturn 1"), name="t", version=1))


def test_rule_ported_lints_codes_and_severities():
    codes = {"monolithic-playbook", "compound-leaf", "agent-does-work", "context-economy"}
    for c in codes:
        src, kw = CORPUS[c]
        r = check(src, name="t", version=1, **kw)
        assert c in _codes(r), c
    assert RULES["monolithic-playbook"].severity == "error"
    assert all(RULES[c].severity == "warning" for c in codes - {"monolithic-playbook"})
    # a single judgment with no quantifier/collection is not monolithic
    single = _pb('return await ctx.llm(f"Draft a reply to {inputs[\'email\']}")')
    assert check(single, name="t", version=1).issues == []
    # looped llm prompts are exempt from compound-leaf / context-economy
    looped = _pb(
        'rows = await ctx.tool("fetch")\nout = []\nfor r in rows["items"]:\n'
        '    out.append(await ctx.llm(f"Summarize {r[\'items\']} one by one"))\nreturn out'
    )
    r = check(looped, name="t", version=1)
    assert not ({"compound-leaf", "context-economy"} & set(_codes(r)))
    # context-economy on an array-typed input and on a whole tool result
    schema = {"type": "object", "properties": {"emails": {"type": "array"}}}
    r = check(
        _pb('await ctx.tool("touch")\nreturn await ctx.llm(f"Summarize {inputs[\'emails\']}")'),
        name="t", version=1, inputs_schema=schema,
    )
    assert "context-economy" in _codes(r)
    r = check(
        _pb('rows = await ctx.tool("fetch")\nreturn await ctx.llm(f"Summarize {rows}")'),
        name="t", version=1,
    )
    assert "context-economy" in _codes(r)


def test_all_issues_at_once():
    src = (
        "import os\n"
        "from datetime import datetime\n"
        "async def run(ctx, inputs):\n"
        "    print(inputs)\n"
        "    started = datetime.now()\n"
        "    url = inputs.url\n"
        '    prompt = "Summarize " + url\n'
        "    s = await ctx.llm(prompt, temperature=0)\n"
        "    try:\n"
        '        rows = await ctx.tool("fetch", url=url)\n'
        "    except:\n"
        "        rows = []\n"
        '    ctx.tool("send_message", text=s)\n'
        "    return {\"rows\": rows, \"started\": started}\n"
    )
    r = check(src, name="t", version=1)
    codes = _codes(r)
    assert {
        "v2-import", "v2-use-ctx-log", "v2-use-ctx-now", "v2-inputs-attr",
        "v2-unknown-kwarg", "v2-broad-except", "v2-missing-await",
    } <= set(codes)
    lines = [i.line for i in r.issues]
    assert lines == sorted(lines)
    assert not r.ok


def test_line_numbers_match_saved_code():
    src = (
        "\n"
        "\n"
        '"""A playbook.\n'
        "\n"
        'Two lines of docstring."""\n'
        "import json\n"
        "\n"
        "async def run(ctx, inputs):\n"
        "    print(json.dumps(inputs))\n"
        "    return 1\n"
    )
    r = check(src, name="t", version=3)
    i = _one(r, "v2-use-ctx-log")
    assert i.line == 9 and i.source_line == "    print(json.dumps(inputs))"
    assert r.filename == "playbook:t@v3" == stable_filename("t", 3)
    co = compile(src, r.filename, "exec")
    assert co.co_filename == r.filename
    run_code = [c for c in co.co_consts if hasattr(c, "co_name") and c.co_name == "run"][0]
    assert run_code.co_firstlineno == 8


def test_syntax_error_is_the_only_issue():
    src = "async def run(ctx, inputs):\n    return (1 +\n"
    r = check(src, name="t", version=1)
    assert _codes(r) == ["v2-syntax"]
    i = r.issues[0]
    assert i.line >= 2 and i.col >= 0
    assert i.source_line == src.splitlines()[i.line - 1]


def test_summary_call_sites():
    src = _pb(
        'a = await ctx.tool("fetch", _id="first")\n'
        'rows = await ctx.tool("fetch")\n'
        'await ctx.tool("touch")\n'
        'await ctx.subtask("child")\n'
        'await ctx.approve(show=rows)\n'
        'for r in rows["items"]:\n'
        '    try:\n'
        '        rows = await ctx.tool("fetch", r=r)\n'
        '    except ctx.ToolError:\n'
        '        await ctx.log("skip")\n'
        'x = await ctx.now()\n'
        'return inputs["k"]'
    )
    r = check(src, name="t", version=1)
    sites = r.summary["call_sites"]
    ids = [c["id"] for c in sites]
    assert ids == ["first", "rows", "touch", "child", "approve", "rows_2", "log", "x"]
    assert all("#" not in i for i in ids)
    assert _one(r, "v2-duplicate-call-site-id").line == sites[5]["line"]
    assert [c["kind"] for c in sites] == ["tool", "tool", "tool", "subtask", "approve", "tool", "log", "now"]
    assert sites[0]["tool"] == "fetch" and sites[0]["playbook"] is None
    assert sites[3]["playbook"] == "child" and sites[3]["tool"] is None
    assert [c["loop_depth"] for c in sites] == [0, 0, 0, 0, 0, 1, 1, 0]
    assert [c["in_try"] for c in sites] == [False, False, False, False, False, True, False, False]
    assert r.summary["inputs_read"] == ["k"]
    assert r.summary["subtasks"] == ["child"]
    assert r.summary["tools"] == ["fetch", "touch"]
    assert set(sites[0]) == {"id", "kind", "line", "col", "tool", "playbook", "loop_depth", "in_try"}


def test_constants_are_consistent():
    assert set(UNAVAILABLE_EFFECTS) == {"sleep"}
    assert "wait_event" in AVAILABLE_EFFECTS and "wait_event" in DEFAULT_FEATURES
    assert not (AVAILABLE_EFFECTS & set(UNAVAILABLE_EFFECTS))
    assert "approval_id" not in APPROVE_RESULT_KEYS


# --------------------------------------------------------------------------- format
@pytest.mark.parametrize("code, expected", [
    ("async def run(ctx, inputs):\n    return 1\n", "python"),
    ("    async def run(ctx, inputs):\n        return 1\n", "python"),
    ('playbook(name="x")\ntool("a")\n', "pblang"),
    ('  playbook(\n    name="x")\n', "pblang"),
    ('async def run(ctx, inputs):\n    return "playbook(name=x)"\n', "python"),
    ("", None),
    ("just some prose about a playbook( that never starts a line\n", None),
])
def test_sniff_format(code, expected):
    assert sniff_format(code) == expected


_PY = "async def run(ctx, inputs):\n    return 1\n"
_PB = 'playbook(name="x")\n'
_NONE = "# nothing\n"


@pytest.mark.parametrize("explicit, code, stored, expected, issue_code", [
    ("python", _PY, None, "python", None),
    ("pblang", _PY, None, "pblang", "v2-format-mismatch"),
    ("python", _PB, None, "python", "v2-format-mismatch"),
    (None, _PY, None, "python", None),
    (None, _PB, None, "pblang", None),
    (None, _NONE, "pblang", "pblang", None),
    (None, _NONE, None, "python", None),
    ("yaml", _PY, None, None, "v2-format-unknown"),
])
def test_resolve_format_table(explicit, code, stored, expected, issue_code):
    fmt, issue = resolve_format(explicit, code, stored=stored)
    assert fmt == expected
    if issue_code is None:
        assert issue is None
    else:
        assert issue is not None and issue.code == issue_code
        assert (issue.line, issue.col) == (1, 0)
        assert issue.severity == "error" and issue.expected and issue.example_fix


def test_resolve_format_default_is_explicit_parameter():
    assert resolve_format(None, _NONE, default="pblang") == ("pblang", None)
