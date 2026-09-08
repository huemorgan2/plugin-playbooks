"""plans/032 phase 01 — docs/v2.md is the contract; the code must match it.

The checker, the shim (phase 02-03) and the skill (phase 05) are written from
docs/v2.md; these checks fail when the doc and `plugin_playbooks.v2` drift.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from plugin_playbooks.v2 import (
    APPROVE_RESULT_KEYS,
    AVAILABLE_EFFECTS,
    CTX_EXCEPTIONS,
    CTX_UNCATCHABLE,
    DEFAULT_TIMEOUTS,
    MAX_EFFECTS,
    UNAVAILABLE_EFFECTS,
)
from plugin_playbooks.v2.checker import RULES, check
from test_no_spec_feature import _BARE_SPEC_RE, _FEATURE_RE

_ROOT = Path(__file__).resolve().parent.parent
_DOC = _ROOT / "docs" / "v2.md"
_V2 = _ROOT / "plugin_playbooks" / "v2"

_HEADINGS = [
    "## 1. Shape", "## 2. Effects", "## 3. Effect options", "## 4. Exceptions",
    "## 5. Determinism", "## 6. Journal", "## 7. Error contract", "## 8. Checker rules",
    "## 9. Format selection", "## 10. Dry run", "## 11. What runs where", "## 12. Constants",
]


def _doc() -> str:
    return _DOC.read_text(encoding="utf-8")


def _section(n: int) -> str:
    text = _doc()
    start = text.index(_HEADINGS[n - 1])
    end = text.index(_HEADINGS[n]) if n < len(_HEADINGS) else len(text)
    return text[start:end]


# Minimal admitted call per available effect (one-line playbooks).
_MINIMAL = {
    "tool": 'await ctx.tool("x")',
    "llm": 'await ctx.llm("p")',
    "agent": 'await ctx.agent("Is this urgent?")',
    "subtask": 'await ctx.subtask("child")',
    "gather": 'await ctx.gather(ctx.tool("x"))',
    "approve": "await ctx.approve(show=1)",
    "now": "await ctx.now()",
    "random": "await ctx.random()",
    "log": 'await ctx.log("m")',
    "wait_event": 'await ctx.wait_event("x", timeout=1)',
}


def test_doc_exists_with_sections():
    assert _DOC.is_file()
    text = _doc()
    headings = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert headings == _HEADINGS


def test_every_doc_effect_has_a_checker_rule_or_is_accepted():
    rows = {
        m.group(1): line
        for line in _section(2).splitlines()
        if (m := re.match(r"^\| `ctx\.(\w+)\(", line))
    }
    assert set(rows) == AVAILABLE_EFFECTS | set(UNAVAILABLE_EFFECTS)
    for eff in UNAVAILABLE_EFFECTS:
        assert "not available" in rows[eff], eff
        src = f"async def run(ctx, inputs):\n    return await ctx.{eff}('x', timeout=1)\n"
        r = check(src, name="t", version=1)
        assert "v2-effect-unavailable" in [i.code for i in r.issues], eff
    for eff in AVAILABLE_EFFECTS:
        src = f"async def run(ctx, inputs):\n    return {_MINIMAL[eff]}\n"
        r = check(src, name="t", version=1)
        assert r.issues == [], (eff, [i.to_dict() for i in r.issues])


def test_doc_exceptions_match_checker():
    sec = _section(4)
    names = set(re.findall(r"`ctx\.([A-Z]\w+)`", sec))
    assert names == CTX_EXCEPTIONS | CTX_UNCATCHABLE
    # the sentence that says "cannot be caught" names exactly the uncatchable three
    sentence = next(s for s in re.split(r"(?<=[.:])\s", sec.replace("\n", " ")) if "cannot be caught" in s)
    for exc in CTX_UNCATCHABLE:
        assert f"`ctx.{exc}`" in sentence, exc
    for exc in CTX_EXCEPTIONS:
        assert f"`ctx.{exc}`" not in sentence, exc


def test_doc_rule_table_matches_RULES():
    rows = re.findall(r"^\| `([a-z0-9-]+)` \| (error|warning) \|", _section(8), re.MULTILINE)
    assert rows, "no rule rows found in §8"
    codes = {c for c, _ in rows}
    assert codes == set(RULES)
    assert len(rows) == len(codes), "duplicate rule rows"
    for code, sev in rows:
        assert RULES[code].severity == sev, code


def test_doc_constants():
    sec = _section(12)
    m = re.search(r"`MAX_EFFECTS = (\d+)`", sec)
    assert m and int(m.group(1)) == MAX_EFFECTS == 200
    m = re.search(r"`DEFAULT_TIMEOUTS = (\{[^`]+\})`", sec)
    assert m
    assert ast.literal_eval(m.group(1)) == DEFAULT_TIMEOUTS
    # §2 repeats the cap in the effect-in-nested-loops row and §5 names it
    assert f"MAX_EFFECTS = {MAX_EFFECTS}" in _section(5)


def test_doc_approve_result_keys_match_constant():
    row = next(ln for ln in _section(2).splitlines() if ln.startswith("| `ctx.approve("))
    cells = row.split(" | ")  # [0] "| Effect", [1] Signature, [2] Returns, ...
    returns = cells[2]
    assert returns.startswith("`{")
    keys = set(re.findall(r'"(\w+)":', returns))
    assert keys == set(APPROVE_RESULT_KEYS)
    assert "approval_id" not in row


def test_doc_examples_pass_clean():
    blocks = re.findall(r"```python\n(.*?)```", _doc(), re.DOTALL)
    assert len(blocks) >= 2
    for block in blocks:
        allowed = set(re.findall(r"#\s*warns:\s*([a-z0-9-]+)", block))
        r = check(block, name="doc", version=1)
        errors = [i.to_dict() for i in r.issues if i.severity == "error"]
        assert errors == [], errors
        warnings = {i.code for i in r.issues if i.severity == "warning"}
        assert warnings <= allowed, (warnings, allowed)
    # the two §1 examples are the checker test fixtures, byte for byte
    from test_v2_checker import EXAMPLE, QUEUE_EXAMPLE

    assert blocks[0] == EXAMPLE
    assert blocks[1] == QUEUE_EXAMPLE


def test_no_spec_feature_tokens():
    files = [_DOC, *sorted(_V2.glob("*.py"))]
    assert len(files) >= 3
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not _FEATURE_RE.search(line), f"{path.name}:{n}: {line!r}"
            assert not _BARE_SPEC_RE.search(line), f"{path.name}:{n}: {line!r}"
            assert "playbook_spec" not in line, f"{path.name}:{n}"
