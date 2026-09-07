"""v2 static checker and format sniff (plans/032 phase 01; contract: docs/v2.md).

Pure AST analysis: nothing here runs, imports or stores playbook code. `check()`
returns every issue at once in the master's shape (line, col, source_line,
message, expected, example_fix, code) plus `severity`; line numbers are the
saved code's because the code is parsed under `stable_filename()` with no
prefix — the same filename the shim compiles under (docs/v2.md §7).

Rule codes and severities live in `RULES`; docs/v2.md §8 is checked against
it by tests/test_v2_contract_doc.py.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from typing import Any

from plugin_playbooks.v2 import (
    AVAILABLE_EFFECTS,
    CTX_EXCEPTIONS,
    CTX_UNCATCHABLE,
    DEFAULT_FEATURES,
    DEFAULT_TIMEOUTS,
    FORMATS,
    MAX_EFFECTS,
    UNAVAILABLE_EFFECTS,
)
from plugin_playbooks.validation import _DEEP_COLLECTION_REF, _prompt_markers

# --------------------------------------------------------------------------- shapes


@dataclass
class CheckIssue:
    line: int
    col: int
    source_line: str
    message: str
    expected: str
    example_fix: str
    code: str
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "col": self.col,
            "source_line": self.source_line,
            "message": self.message,
            "expected": self.expected,
            "example_fix": self.example_fix,
            "code": self.code,
            "severity": self.severity,
        }


@dataclass
class CheckResult:
    issues: list[CheckIssue]
    summary: dict[str, Any]
    filename: str
    ok: bool


@dataclass(frozen=True)
class Rule:
    severity: str  # "error" | "warning"
    title: str


RULES: dict[str, Rule] = {
    "v2-syntax": Rule("error", "the code does not parse"),
    "v2-entry-point": Rule("error", "exactly one top-level `async def run(ctx, inputs)`"),
    "v2-top-level": Rule("error", "only a docstring, imports, helpers and literal constants at top level; no classes"),
    "v2-import": Rule("error", "import outside the stdlib whitelist"),
    "v2-banned-name": Rule("error", "exec/eval/open/introspection builtins and dunder attributes"),
    "v2-use-ctx-now": Rule("error", "clock reads go through ctx.now()"),
    "v2-use-ctx-random": Rule("error", "randomness goes through ctx.random()"),
    "v2-use-ctx-log": Rule("error", "print is silenced; use ctx.log()"),
    "v2-use-ctx-gather": Rule("error", "concurrency goes through ctx.gather()"),
    "v2-effect-unavailable": Rule("error", "effect not available in this version"),
    "v2-unknown-ctx-attr": Rule("error", "unknown attribute on ctx"),
    "v2-effect-not-on-ctx": Rule("error", "effects are called on ctx only; no pblang step functions"),
    "v2-missing-await": Rule("error", "an effect call must be awaited (or handed to ctx.gather)"),
    "v2-tool-name-literal": Rule("error", "ctx.tool / ctx.subtask / ctx.wait_event take a string literal name"),
    "v2-unknown-kwarg": Rule("error", "argument outside the effect signature"),
    "v2-option": Rule("error", "bad effect option (_id / _timeout / _retry)"),
    "v2-duplicate-call-site-id": Rule("warning", "two call sites resolve to the same id"),
    "v2-unknown-tool": Rule("error", "tool name not in the registry"),
    "v2-broad-except": Rule("error", "bare/broad except around an effect"),
    "v2-uncatchable": Rule("error", "catching an uncatchable ctx exception"),
    "v2-inputs-attr": Rule("error", "inputs is a dict; steps/vars/event do not exist in v2"),
    "v2-unknown-input": Rule("warning", "input key not declared in the manifest schema"),
    "v2-set-iteration": Rule("warning", "iterating a set is not replayable; sort it"),
    "v2-nondeterministic-builtin": Rule("warning", "hash()/id() differ per process"),
    "v2-too-many-call-sites": Rule("warning", "more than 40 static effect call sites"),
    "v2-effect-in-nested-loops": Rule("warning", "effect inside nested loops"),
    "v2-gather-args": Rule("error", "ctx.gather takes un-awaited effect calls only"),
    "v2-async-construct": Rule("error", "async for / async with are not available"),
    "monolithic-playbook": Rule("error", "the whole task delegated to a single llm/agent call"),
    "compound-leaf": Rule("warning", "one prompt hides a loop or several operations"),
    "agent-does-work": Rule("warning", "ctx.agent doing mechanical work with no judgment"),
    "context-economy": Rule("warning", "a whole collection fed into one model call"),
    "v2-format-mismatch": Rule("error", "explicit format disagrees with the code"),
    "v2-format-unknown": Rule("error", "format is not one of pblang / python"),
}

# --------------------------------------------------------------------------- tables

IMPORT_WHITELIST = frozenset({
    "json", "re", "math", "datetime", "textwrap", "itertools", "collections",
    "functools", "operator", "string", "decimal", "fractions", "statistics",
    "typing", "dataclasses", "enum", "base64", "hashlib", "html", "difflib",
    "heapq", "bisect", "copy", "unicodedata",
})
_IMPORT_STEERS: dict[str, tuple[str, str]] = {
    "time": ("the clock is `await ctx.now()` (journaled, replayed)", "started = await ctx.now()"),
    "random": ("randomness is `await ctx.random()` (journaled, replayed)", "r = await ctx.random()"),
    "uuid": ("ids come from `await ctx.random()` (journaled, replayed)", "token = str(await ctx.random())"),
    "secrets": ("randomness is `await ctx.random()` (journaled, replayed)", "r = await ctx.random()"),
    "asyncio": (
        "concurrency is `await ctx.gather(...)`; ctx.sleep is not available in this version",
        'a, b = await ctx.gather(ctx.tool("a"), ctx.tool("b"))',
    ),
}
_NO_IO_MODULES = frozenset({
    "os", "sys", "subprocess", "socket", "pathlib", "io", "requests", "httpx", "urllib",
    "shutil", "glob", "tempfile", "threading", "multiprocessing", "importlib", "ctypes",
})
BANNED_CALLS = frozenset({
    "open", "exec", "eval", "compile", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "exit", "quit", "help",
})
BANNED_ATTRS = frozenset({"__class__", "__dict__", "__globals__", "__builtins__", "__subclasses__"})
_CLOCK_CALLS = (
    "datetime.now", "datetime.utcnow", "datetime.today", "date.today",
    "time.time", "time.monotonic", "time.perf_counter",
)
_RANDOM_MODULES = ("random", "uuid", "secrets")
_ASYNCIO_GATHER = frozenset({"gather", "create_task", "wait", "as_completed"})
# pblang step-name table (pblang/compiler.py STEP_FUNCS) + the header.
_PBLANG_NAMES = frozenset({
    "tool", "llm", "agent", "if_", "loop", "parallel", "approve", "wait_event",
    "subtask", "state", "halt", "code", "playbook",
})
_EFFECT_NAMES = AVAILABLE_EFFECTS | frozenset(UNAVAILABLE_EFFECTS)
_KNOWN_CTX_ATTRS = _EFFECT_NAMES | CTX_EXCEPTIONS | CTX_UNCATCHABLE
_DICT_METHODS = frozenset({
    "get", "keys", "values", "items", "copy", "setdefault", "pop", "update", "__contains__",
})
_V1_NAMESPACES = frozenset({"steps", "vars", "event"})
_NONDET_BUILTINS = frozenset({"hash", "id"})
MAX_CALL_SITES = 40
NESTED_LOOP_DEPTH = 2

# Effect signatures: positional names (in order), extra keyword names, required.
_SIGNATURES: dict[str, dict[str, Any]] = {
    "tool": {"positional": ["name"], "keywords": None, "required": ["name"]},
    "llm": {"positional": ["prompt"], "keywords": ["output", "purpose", "model", "system"], "required": ["prompt"]},
    "agent": {"positional": ["prompt"], "keywords": ["output", "tools"], "required": ["prompt"]},
    "subtask": {"positional": ["playbook", "inputs"], "keywords": ["returns"], "required": ["playbook"]},
    "approve": {"positional": [], "keywords": ["show"], "required": ["show"]},
    "wait_event": {"positional": ["name"], "keywords": ["filter", "timeout"], "required": ["name", "timeout"]},
    "now": {"positional": [], "keywords": [], "required": []},
    "random": {"positional": [], "keywords": [], "required": []},
    "log": {"positional": ["msg"], "keywords": [], "required": ["msg"]},
    "gather": {"positional": [], "keywords": [], "required": []},
}
_OPTIONS: dict[str, frozenset[str]] = {
    "tool": frozenset({"_id", "_timeout", "_retry"}),
    "llm": frozenset({"_id", "_timeout", "_retry"}),
    "agent": frozenset({"_id", "_timeout", "_retry"}),
    "subtask": frozenset({"_id", "_timeout"}),
    "approve": frozenset({"_id"}),
    "wait_event": frozenset({"_id"}),
    "now": frozenset({"_id"}),
    "random": frozenset({"_id"}),
    "log": frozenset({"_id"}),
    "gather": frozenset(),
}
_LITERAL_NAME_KINDS = frozenset({"tool", "subtask", "wait_event"})
_WORK_KINDS = frozenset({"tool", "llm", "agent", "subtask"})
_DELIVERY_TOOL = "send_chat_message"

_PY_RE = re.compile(r"^\s*async def run\(", re.MULTILINE)
_PB_RE = re.compile(r"^\s*playbook\(", re.MULTILINE)


# --------------------------------------------------------------------------- format


def stable_filename(name: str, version: int | str) -> str:
    """The filename the checker parses under and the shim compiles under (§7)."""
    return f"playbook:{name}@v{version}"


def sniff_format(code: str) -> str | None:
    if _PY_RE.search(code or ""):
        return "python"
    if _PB_RE.search(code or ""):
        return "pblang"
    return None


def resolve_format(
    explicit: str | None, code: str, *, stored: str | None = None, default: str = "python",
) -> tuple[str | None, CheckIssue | None]:
    """Precedence (§9): explicit > sniff > stored (edit) > default (propose)."""
    first = (code or "").splitlines()[0] if (code or "").splitlines() else ""
    if explicit is not None:
        if explicit not in FORMATS:
            return None, CheckIssue(
                1, 0, first,
                f"format {explicit!r} is not one of {', '.join(FORMATS)}",
                "format='python' (an `async def run(ctx, inputs)` playbook) or format='pblang'",
                'format="python"', "v2-format-unknown", "error",
            )
        sniffed = sniff_format(code)
        if sniffed is not None and sniffed != explicit:
            return explicit, CheckIssue(
                1, 0, first,
                f"format={explicit!r} but the code looks like {sniffed} "
                f"({'starts with `async def run(`' if sniffed == 'python' else 'starts with `playbook(`'})",
                f"format={sniffed!r}, or code written in {explicit}",
                f'format="{sniffed}"', "v2-format-mismatch", "error",
            )
        return explicit, None
    sniffed = sniff_format(code)
    if sniffed is not None:
        return sniffed, None
    if stored is not None:
        return stored, None
    return default, None


# --------------------------------------------------------------------------- AST helpers


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` for a Name/Attribute chain, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _is_ctx_attr(node: ast.AST, attr: str | None = None) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "ctx"
        and (attr is None or node.attr == attr)
    )


def _effect_kind(call: ast.AST) -> str | None:
    """The `ctx.<effect>` kind of a Call node (available or unavailable), else None."""
    if isinstance(call, ast.Call) and _is_ctx_attr(call.func) and call.func.attr in _EFFECT_NAMES:
        return call.func.attr
    return None


def _is_gather_call(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Call) and _is_ctx_attr(node.func, "gather")


def _is_literal(node: ast.AST) -> bool:
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False
    return True


def _is_set_expr(node: ast.AST) -> bool:
    return (
        isinstance(node, (ast.Set, ast.SetComp))
        or (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "set")
    )


def _walk_shallow(node: ast.AST):
    """Descendants of `node` without entering nested function/lambda/class bodies."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _prompt_text(node: ast.AST | None) -> str | None:
    """Constant prompt text: a string constant, the constant parts of an
    f-string, or a `+` of those. None when the prompt is not static."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return " ".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _prompt_text(node.left), _prompt_text(node.right)
        if left is None and right is None:
            return None
        return (left or "") + " " + (right or "")
    return None


def _interpolations(node: ast.AST | None) -> list[ast.AST]:
    if isinstance(node, ast.JoinedStr):
        return [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _interpolations(node.left) + _interpolations(node.right)
    return []


def _is_collection_word(word: str) -> bool:
    # reuse the v1 table: the regex accepts `steps.x.<word>` iff word is a collection hint
    return _DEEP_COLLECTION_REF.search(f"steps.x.{word}") is not None


# --------------------------------------------------------------------------- the checker


class _Tree:
    """Parent/field maps over one parsed module plus the context helpers."""

    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree
        self.parent: dict[int, ast.AST] = {}
        self.field: dict[int, str] = {}
        for node in ast.walk(tree):
            for name, value in ast.iter_fields(node):
                if isinstance(value, ast.AST):
                    self.parent[id(value)] = node
                    self.field[id(value)] = name
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, ast.AST):
                            self.parent[id(item)] = node
                            self.field[id(item)] = name

    def parent_of(self, node: ast.AST) -> ast.AST | None:
        return self.parent.get(id(node))

    def field_of(self, node: ast.AST) -> str | None:
        return self.field.get(id(node))

    def scope_of(self, node: ast.AST) -> ast.AST:
        p = self.parent_of(node)
        while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            p = self.parent_of(p)
        return p if p is not None else self.tree

    def loop_depth(self, node: ast.AST) -> int:
        d = 0
        child = node
        p = self.parent_of(child)
        while p is not None:
            f = self.field_of(child)
            if isinstance(p, (ast.For, ast.AsyncFor, ast.While)) and f in ("body", "orelse"):
                d += 1
            elif isinstance(p, ast.comprehension):
                comp = self.parent_of(p)
                first = getattr(comp, "generators", [None])[0]
                if not (f == "iter" and first is p):
                    d += 1
            elif isinstance(p, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) \
                    and f in ("elt", "key", "value"):
                d += 1
            child, p = p, self.parent_of(p)
        return d

    def try_ancestors(self, node: ast.AST) -> list[ast.AST]:
        out: list[ast.AST] = []
        child = node
        p = self.parent_of(child)
        while p is not None:
            if isinstance(p, (ast.Try, getattr(ast, "TryStar", ast.Try))) and self.field_of(child) == "body":
                out.append(p)
            child, p = p, self.parent_of(p)
        return out


class _Checker:
    def __init__(
        self, code: str, *, name: str, version: int | str, inputs_schema: dict | None,
        tool_names: set[str] | None, features: Any,
    ) -> None:
        self.code = code
        self.lines = code.splitlines()
        self.filename = stable_filename(name, version)
        self.inputs_props: dict | None = None
        if isinstance(inputs_schema, dict) and isinstance(inputs_schema.get("properties"), dict):
            self.inputs_props = inputs_schema["properties"]
        self.tool_names = set(tool_names) if tool_names is not None else None
        self.features = frozenset(features or ())
        self.available = set(AVAILABLE_EFFECTS)
        if "wait_event" in self.features:
            self.available.add("wait_event")
        self.issues: list[CheckIssue] = []
        self.imports: list[str] = []
        self.import_names: set[str] = set()  # bound module names / aliases
        self.call_sites: list[dict[str, Any]] = []
        self.inputs_read: set[str] = set()
        self.tools: set[str] = set()
        self.subtasks: set[str] = set()
        self.run_def: ast.AsyncFunctionDef | None = None

    # -- plumbing ---------------------------------------------------------
    def _src(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1]
        return ""

    def issue(self, code: str, node: ast.AST | None, message: str, expected: str, example_fix: str,
              *, line: int | None = None, col: int | None = None) -> None:
        if node is not None:
            line = getattr(node, "lineno", 0) if line is None else line
            col = getattr(node, "col_offset", 0) if col is None else col
        line = line or 0
        col = col or 0
        self.issues.append(CheckIssue(
            line, col, self._src(line), message, expected, example_fix, code, RULES[code].severity,
        ))

    # -- driver -----------------------------------------------------------
    def run(self) -> CheckResult:
        try:
            tree = ast.parse(self.code, self.filename)
        except SyntaxError as e:
            line = e.lineno or 0
            col = max((e.offset or 1) - 1, 0)
            self.issue(
                "v2-syntax", None, f"Python syntax error: {e.msg}",
                "code that parses — plain Python, one `async def run(ctx, inputs)`",
                "async def run(ctx, inputs):\n    rows = await ctx.tool(\"fetch\")\n    return rows",
                line=line, col=col,
            )
            return self._result()
        self.t = _Tree(tree)
        self._top_level(tree)
        self._entry_point(tree)
        self._walk(tree)
        self._ported_lints()
        return self._result()

    def _result(self) -> CheckResult:
        self.issues.sort(key=lambda i: (i.line, i.col, i.code))
        summary = {
            "format": "python",
            "tools": sorted(self.tools),
            "subtasks": sorted(self.subtasks),
            "call_sites": self.call_sites,
            "inputs_read": sorted(self.inputs_read),
            "imports": sorted(self.imports),
        }
        ok = not any(i.severity == "error" for i in self.issues)
        return CheckResult(self.issues, summary, self.filename, ok)

    # -- R3 / R4 ------------------------------------------------------------
    def _top_level(self, tree: ast.Module) -> None:
        for idx, node in enumerate(tree.body):
            if idx == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._import(node)
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) and _is_literal(node.value):
                continue
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.value is not None and _is_literal(node.value):
                continue
            what = "a class" if isinstance(node, ast.ClassDef) else f"a top-level `{type(node).__name__}` statement"
            self.issue(
                "v2-top-level", node,
                f"{what} is not allowed at module level — only a docstring, whitelisted imports, "
                "`def`/`async def` helpers and `NAME = <literal>` constants",
                "module body = docstring | import | def helper | NAME = literal | async def run",
                "LIMIT = 3\n\ndef helper(x):\n    return x * 2\n\nasync def run(ctx, inputs):\n    return helper(LIMIT)",
            )
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and self.t.parent_of(node) is not tree:
                self.issue(
                    "v2-top-level", node, "classes are not allowed in a playbook",
                    "plain functions and dicts", "def helper(x):\n    return {\"value\": x}",
                )

    def _import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.ImportFrom):
            if node.level:
                self.issue(
                    "v2-import", node, "relative imports are not allowed",
                    f"one of: {', '.join(sorted(IMPORT_WHITELIST))}", "import json",
                )
                return
            roots = [(node.module or "").split(".")[0]]
            bound = [a.asname or a.name for a in node.names]
        else:
            roots = [a.name.split(".")[0] for a in node.names]
            bound = [a.asname or a.name.split(".")[0] for a in node.names]
        for root in roots:
            if root in IMPORT_WHITELIST:
                self.imports.append(root)
                self.import_names.update(bound)
                continue
            self.import_names.update(bound)
            if root in _IMPORT_STEERS:
                why, fix = _IMPORT_STEERS[root]
                self.issue(
                    "v2-import", node, f"`import {root}` is not allowed: {why}", why, fix,
                )
            elif root in _NO_IO_MODULES:
                self.issue(
                    "v2-import", node,
                    f"`import {root}` is not allowed: no I/O; the world is reached through ctx.tool",
                    "the world is reached through `await ctx.tool(<name>, ...)`",
                    'page = await ctx.tool("http_request", url=inputs["url"])',
                )
            else:
                self.issue(
                    "v2-import", node,
                    f"`import {root}` is not allowed: module not in the whitelist",
                    f"one of: {', '.join(sorted(IMPORT_WHITELIST))}", "import json",
                )

    # -- R2 -------------------------------------------------------------------
    def _entry_point(self, tree: ast.Module) -> None:
        expected = "exactly one top-level `async def run(ctx, inputs)`"
        fix = "async def run(ctx, inputs):\n    ...\n    return {\"count\": 1}"
        runs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run"]
        if not runs:
            self.issue("v2-entry-point", None, "no `async def run(ctx, inputs)` found", expected, fix, line=0, col=0)
            return
        for extra in runs[1:]:
            self.issue("v2-entry-point", extra, "`run` is defined more than once", expected, fix)
        run = runs[0]
        if isinstance(run, ast.FunctionDef):
            self.issue("v2-entry-point", run, "`run` must be `async def run(ctx, inputs)` (it is a plain `def`)", expected, fix)
            return
        a = run.args
        names = [x.arg for x in a.posonlyargs + a.args]
        problems: list[str] = []
        if a.posonlyargs:
            problems.append("no positional-only marker")
        if names != ["ctx", "inputs"]:
            problems.append(f"parameters must be exactly (ctx, inputs), got ({', '.join(names)})")
        if a.defaults or a.kw_defaults:
            problems.append("no default values")
        if a.vararg or a.kwarg or a.kwonlyargs:
            problems.append("no *args / **kwargs / keyword-only parameters")
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in _walk_shallow(run)):
            problems.append("`run` must not `yield` (it is a coroutine, not a generator)")
        if problems:
            self.issue("v2-entry-point", run, "bad `run` signature: " + "; ".join(problems), expected, fix)
        self.run_def = run

    # -- the walk ---------------------------------------------------------------
    def _walk(self, tree: ast.Module) -> None:
        t = self.t
        effect_calls: list[ast.Call] = []
        seen_ids: dict[str, int] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFor, ast.AsyncWith)):
                self.issue(
                    "v2-async-construct", node,
                    f"`{'async for' if isinstance(node, ast.AsyncFor) else 'async with'}` is not available — "
                    "the only awaitables are ctx effects",
                    "a plain `for` loop over data; effects via `await ctx.<effect>(...)`",
                    'for item in inputs["items"]:\n    await ctx.tool("touch", item=item)',
                )
            if isinstance(node, ast.Attribute):
                self._attribute(node)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                self._name_load(node)
            if isinstance(node, ast.Subscript):
                self._subscript(node)
            if isinstance(node, ast.ExceptHandler):
                self._handler(node)
            if isinstance(node, (ast.For, ast.comprehension)):
                self._iteration(node.iter, node)
            if isinstance(node, ast.Call):
                self._call(node)
                if _effect_kind(node) is not None:
                    effect_calls.append(node)
        effect_calls.sort(key=lambda c: (c.lineno, c.col_offset))
        for call in effect_calls:
            self._effect(call, seen_ids)
        if len(self.call_sites) > MAX_CALL_SITES:
            site = self.call_sites[MAX_CALL_SITES]
            self.issue(
                "v2-too-many-call-sites", None,
                f"{len(self.call_sites)} static effect call sites (more than {MAX_CALL_SITES}) — "
                "split the playbook with ctx.subtask or loop over data",
                f"at most {MAX_CALL_SITES} effect call sites per playbook",
                'for item in items:\n    await ctx.tool("touch", item=item)  # one site, many occurrences',
                line=site["line"], col=site["col"],
            )

    # -- R5 / R11 / R20 -----------------------------------------------------------
    def _attribute(self, node: ast.Attribute) -> None:
        if node.attr in BANNED_ATTRS:
            self.issue(
                "v2-banned-name", node, f"`.{node.attr}` access is not allowed",
                "no introspection — plain data access", 'value = row["field"]',
            )
        if isinstance(node.value, ast.Name) and node.value.id == "ctx":
            if node.attr not in _KNOWN_CTX_ATTRS:
                close = difflib.get_close_matches(node.attr, sorted(_KNOWN_CTX_ATTRS), n=1)
                hint = f" — did you mean `ctx.{close[0]}`?" if close else ""
                self.issue(
                    "v2-unknown-ctx-attr", node, f"`ctx.{node.attr}` does not exist{hint}",
                    "effects: " + ", ".join(sorted(self.available)) + "; exceptions: "
                    + ", ".join(sorted(CTX_EXCEPTIONS | CTX_UNCATCHABLE)),
                    f'await ctx.{close[0] if close else "tool"}(...)',
                )
        if isinstance(node.value, ast.Name) and node.value.id == "inputs" and node.attr not in _DICT_METHODS \
                and node.attr not in BANNED_ATTRS:
            self.issue(
                "v2-inputs-attr", node,
                f"`inputs.{node.attr}` — `inputs` is a dict in v2, not a pblang namespace",
                f'inputs["{node.attr}"]', f'value = inputs["{node.attr}"]',
            )

    def _name_load(self, node: ast.Name) -> None:
        if node.id in _V1_NAMESPACES and not self._bound(node.id, self.t.scope_of(node)):
            self.issue(
                "v2-inputs-attr", node,
                f"`{node.id}` — no such namespace in v2; results live in ordinary variables",
                "`rows = await ctx.tool(...)` then `rows[...]`; inputs via `inputs[\"key\"]`",
                'rows = await ctx.tool("fetch")\nfirst = rows["items"][0]',
            )
        if node.id in BANNED_ATTRS:
            self.issue(
                "v2-banned-name", node, f"`{node.id}` is not allowed",
                "no introspection — plain data access", 'value = row["field"]',
            )

    def _bound(self, name: str, scope: ast.AST) -> bool:
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = scope.args
            if name in [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs] \
                    or (a.vararg and a.vararg.arg == name) or (a.kwarg and a.kwarg.arg == name):
                return True
        for n in ast.walk(scope):
            if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store):
                return True
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
                return True
        return False

    def _subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Name) and node.value.id == "inputs" \
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            self._input_key(node.slice.value, node)

    def _input_key(self, key: str, node: ast.AST) -> None:
        self.inputs_read.add(key)
        if self.inputs_props is not None and key not in self.inputs_props:
            known = ", ".join(sorted(self.inputs_props)) or "(no inputs declared)"
            self.issue(
                "v2-unknown-input", node,
                f'inputs["{key}"] is not declared in the inputs schema',
                f"one of: {known}", f'value = inputs["{sorted(self.inputs_props)[0] if self.inputs_props else key}"]',
            )

    # -- R18 / R19 -------------------------------------------------------------------
    def _handler(self, node: ast.ExceptHandler) -> None:
        types: list[ast.AST] = []
        if node.type is None:
            broad = True
        else:
            types = list(node.type.elts) if isinstance(node.type, ast.Tuple) else [node.type]
            broad = any(isinstance(x, ast.Name) and x.id in ("Exception", "BaseException") for x in types)
        for x in types:
            if _is_ctx_attr(x) and x.attr in CTX_UNCATCHABLE:
                self.issue(
                    "v2-uncatchable", x,
                    f"`ctx.{x.attr}` derives from BaseException and cannot be caught — the run ends there",
                    "catch ctx.EffectError (or a subclass: " + ", ".join(sorted(CTX_EXCEPTIONS - {"EffectError"})) + ")",
                    'try:\n    rows = await ctx.tool("fetch")\nexcept ctx.ToolError as e:\n    rows = []',
                )
        if not broad:
            return
        tr = self.t.parent_of(node)
        if tr is None or not any(_effect_kind(c) is not None for c in self._try_body(tr)):
            return
        what = "bare `except:`" if node.type is None else f"`except {_dotted(types[0]) if len(types) == 1 else 'Exception'}`"
        self.issue(
            "v2-broad-except", node,
            f"{what} around an effect also swallows run cancellation and journal errors — "
            "catch ctx.EffectError (or ctx.ToolError, ctx.EffectTimeout, ctx.OutcomeUnknown, …)",
            "except ctx.EffectError as e:  (or a specific ctx.ToolError / ctx.EffectTimeout / ctx.Rejected …)",
            'try:\n    rows = await ctx.tool("fetch")\nexcept ctx.ToolError as e:\n    rows = []',
        )

    @staticmethod
    def _try_body(tr: ast.AST) -> set[ast.AST]:
        out: set[ast.AST] = set()
        for stmt in getattr(tr, "body", []):
            out.add(stmt)
            out.update(ast.walk(stmt))
        return out

    # -- R22 --------------------------------------------------------------------------
    def _iteration(self, it: ast.AST, node: ast.AST) -> None:
        if self._is_set_valued(it, node):
            self.issue(
                "v2-set-iteration", it,
                "iterating a set: element order differs between segments, so a resumed run diverges",
                "sorted(<the set>) — a stable order", "for x in sorted(seen):\n    ...",
            )

    def _is_set_valued(self, expr: ast.AST, at: ast.AST) -> bool:
        if _is_set_expr(expr):
            return True
        if isinstance(expr, ast.Name):
            scope = self.t.scope_of(at)
            for n in ast.walk(scope):
                if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                        and n.targets[0].id == expr.id and _is_set_expr(n.value):
                    return True
        return False

    # -- calls: R5-R10, R12, R23, R22 (join/list) ---------------------------------------
    def _call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            n = func.id
            if n in BANNED_CALLS:
                self.issue(
                    "v2-banned-name", node, f"`{n}(` is not allowed in a playbook",
                    "no files, no exec/eval, no introspection — data comes from ctx.tool",
                    'data = await ctx.tool("read_file", path=inputs["path"])',
                )
            elif n == "print":
                self.issue(
                    "v2-use-ctx-log", node, "`print` output is silenced; use `await ctx.log(msg)`",
                    "await ctx.log(<message>)", 'await ctx.log(f"fetched {len(rows)} rows")',
                )
            elif n in _NONDET_BUILTINS:
                self.issue(
                    "v2-nondeterministic-builtin", node,
                    f"`{n}()` differs per process — a resumed run would take a different path",
                    "a value derived from the data itself (a key, an index, a string)",
                    'key = row["id"]',
                )
            elif n in _PBLANG_NAMES:
                self.issue(
                    "v2-effect-not-on-ctx", node,
                    f"`{n}(` is a pblang step function; in a python playbook effects are `await ctx.<effect>(...)`",
                    "await ctx.tool(...), ctx.llm(...), ctx.agent(...), ctx.approve(...), ctx.subtask(...), ctx.gather(...)",
                    'rows = await ctx.tool("fetch_list", url=inputs["url"])',
                )
            elif n in ("list", "tuple", "enumerate") and node.args and self._is_set_valued(node.args[0], node):
                self._iteration(node.args[0], node)
            return
        if isinstance(func, ast.Attribute):
            dotted = _dotted(func) or ""
            root = dotted.split(".")[0]
            if func.attr == "join" and len(node.args) == 1 and self._is_set_valued(node.args[0], node):
                self._iteration(node.args[0], node)
            if any(dotted.endswith(c) for c in _CLOCK_CALLS):
                self.issue(
                    "v2-use-ctx-now", node,
                    f"`{dotted}()` reads the real clock — a resumed run would see a different time",
                    "now = await ctx.now()  (journaled, replayed)", "now = await ctx.now()",
                )
            elif root in _RANDOM_MODULES:
                self.issue(
                    "v2-use-ctx-random", node,
                    f"`{dotted}()` is not replayable — use `await ctx.random()`",
                    "r = await ctx.random()  (float in [0, 1), journaled)",
                    "r = await ctx.random()\npick = items[int(r * len(items))]",
                )
            elif dotted in ("asyncio.sleep", "time.sleep"):
                self.issue(
                    "v2-effect-unavailable", node,
                    f"`{dotted}` — sleeping is not available in this version",
                    "no sleep; return and let a trigger re-run the playbook later",
                    'await ctx.tool("send_message", to=inputs["owner"], text="done")',
                )
            elif root == "asyncio" and func.attr in _ASYNCIO_GATHER:
                self.issue(
                    "v2-use-ctx-gather", node,
                    f"`{dotted}` is not available — concurrency is `await ctx.gather(...)`",
                    "results = await ctx.gather(ctx.tool(...), ctx.tool(...))",
                    'a, b = await ctx.gather(ctx.tool("fetch", id=1), ctx.tool("fetch", id=2))',
                )
            elif func.attr in _EFFECT_NAMES and isinstance(func.value, ast.Name) and func.value.id != "ctx" \
                    and func.value.id not in self.import_names and root not in IMPORT_WHITELIST \
                    and root not in _IMPORT_STEERS and root not in _NO_IO_MODULES:
                self.issue(
                    "v2-effect-not-on-ctx", node,
                    f"`{dotted}(` — effects are called on `ctx` only",
                    f"await ctx.{func.attr}(...)", f"result = await ctx.{func.attr}(...)",
                )
            elif func.attr == "get" and isinstance(func.value, ast.Name) and func.value.id == "inputs" \
                    and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                self._input_key(node.args[0].value, node)

    # -- effects: R10, R13-R17, R25, R30, call sites ---------------------------------------
    def _effect(self, call: ast.Call, seen_ids: dict[str, int]) -> None:
        kind = call.func.attr  # type: ignore[union-attr]
        t = self.t
        if kind not in self.available:
            reason = UNAVAILABLE_EFFECTS.get(kind, "not available in this version")
            self.issue(
                "v2-effect-unavailable", call, f"`ctx.{kind}` is {reason}",
                "one of: " + ", ".join(sorted(self.available)),
                'await ctx.tool("send_message", to=inputs["owner"], text="done")',
            )
            return
        sig = _SIGNATURES[kind]
        options = _OPTIONS[kind]
        # R13 — awaited or gather-bound
        if not self._await_ok(call):
            self.issue(
                "v2-missing-await", call,
                f"`ctx.{kind}(...)` is not awaited — nothing runs and the result is a coroutine",
                f"result = await ctx.{kind}(...)  or  await ctx.gather(ctx.{kind}(...), ...)",
                self._example_awaited(call, kind),
            )
        # R30 — gather arguments
        if kind == "gather":
            for arg in call.args:
                if isinstance(arg, ast.Starred):
                    continue
                k = _effect_kind(arg)
                if k is None or k == "gather" or not isinstance(arg, ast.Call):
                    self.issue(
                        "v2-gather-args", arg,
                        "ctx.gather takes un-awaited effect calls only — not "
                        + ("a nested ctx.gather" if k == "gather" else "a plain value / helper coroutine / awaited result"),
                        "ctx.gather(ctx.tool(...), ctx.llm(...), *[ctx.tool(...) for x in xs])",
                        'a, b = await ctx.gather(ctx.tool("fetch", id=1), ctx.tool("fetch", id=2))',
                    )
            for kw in call.keywords:
                self.issue(
                    "v2-option", kw, f"ctx.gather takes no options or keywords (`{kw.arg or '**'}`)",
                    "ctx.gather(<effect call>, ...)", 'await ctx.gather(ctx.tool("a"), ctx.tool("b"))',
                )
            return
        # R14 — literal names
        literal: str | None = None
        positional = [a for a in call.args]
        if kind in _LITERAL_NAME_KINDS:
            if not positional or isinstance(positional[0], ast.Starred) \
                    or not (isinstance(positional[0], ast.Constant) and isinstance(positional[0].value, str)):
                self.issue(
                    "v2-tool-name-literal", call,
                    f"ctx.{kind} needs a string literal as its first argument (the {sig['positional'][0]})",
                    f'ctx.{kind}("<{sig["positional"][0]}>", ...)',
                    'rows = await ctx.tool("fetch_list", url=inputs["url"])' if kind == "tool"
                    else f'result = await ctx.{kind}("child", ...)',
                )
            else:
                literal = positional[0].value
            if kind == "tool" and len(positional) > 1:
                self.issue(
                    "v2-tool-name-literal", positional[1],
                    "ctx.tool takes the name and then keyword arguments only",
                    'ctx.tool("<name>", key=value, ...)', 'rows = await ctx.tool("fetch_list", url=inputs["url"])',
                )
        # R15 — signature
        if kind != "tool":
            if len(positional) > len(sig["positional"]) or any(isinstance(a, ast.Starred) for a in positional):
                self.issue(
                    "v2-unknown-kwarg", call,
                    f"ctx.{kind} takes {len(sig['positional'])} positional argument(s)"
                    + (f" ({', '.join(sig['positional'])})" if sig["positional"] else ""),
                    self._signature_text(kind), self._signature_example(kind),
                )
        given = {n: True for n in sig["positional"][: len(positional)]}
        for kw in call.keywords:
            if kw.arg is None:
                if kind != "tool":
                    self.issue(
                        "v2-unknown-kwarg", kw, f"`**` splat is not allowed on ctx.{kind}",
                        self._signature_text(kind), self._signature_example(kind),
                    )
                continue
            if kw.arg.startswith("_"):
                self._option(kind, kw, options)
                continue
            given[kw.arg] = True
            if kind == "tool":
                continue
            allowed = set(sig["positional"]) | set(sig["keywords"])
            if kw.arg not in allowed:
                self.issue(
                    "v2-unknown-kwarg", kw,
                    f"ctx.{kind} has no argument `{kw.arg}`",
                    self._signature_text(kind), self._signature_example(kind),
                )
        for req in sig["required"]:
            if req not in given:
                if kind in _LITERAL_NAME_KINDS and req == sig["positional"][0]:
                    continue  # R14 already said so
                self.issue(
                    "v2-unknown-kwarg", call,
                    f"ctx.{kind} requires `{req}`" + (" — no unbounded waits" if req == "timeout" else ""),
                    self._signature_text(kind), self._signature_example(kind),
                )
        # R17
        if kind == "tool" and literal is not None:
            self.tools.add(literal)
            if self.tool_names is not None and literal not in self.tool_names:
                close = difflib.get_close_matches(literal, sorted(self.tool_names), n=3)
                self.issue(
                    "v2-unknown-tool", positional[0],
                    f'tool "{literal}" is not registered' + (f" — did you mean {', '.join(close)}?" if close else ""),
                    "one of: " + ", ".join(sorted(self.tool_names)),
                    f'rows = await ctx.tool("{close[0] if close else sorted(self.tool_names)[0]}", ...)',
                )
        if kind == "subtask" and literal is not None:
            self.subtasks.add(literal)
        # call site
        depth = t.loop_depth(call)
        in_try = bool(t.try_ancestors(call))
        raw_id = self._call_site_id(call, kind, literal)
        n = seen_ids.get(raw_id, 0) + 1
        seen_ids[raw_id] = n
        site_id = raw_id if n == 1 else f"{raw_id}_{n}"
        if n > 1:
            self.issue(
                "v2-duplicate-call-site-id", call,
                f'call-site id "{raw_id}" is already used by an earlier effect — this one is "{site_id}"',
                "a unique `_id=` per call site", f'await ctx.{kind}(..., _id="{raw_id}_{kind}")',
            )
        if depth >= NESTED_LOOP_DEPTH:
            self.issue(
                "v2-effect-in-nested-loops", call,
                f"effect inside {depth} nested loops — every occurrence is a journal row and the run "
                f"is capped at MAX_EFFECTS = {MAX_EFFECTS}",
                "flatten the work (one loop, or batch the inner items into one effect)",
                'pairs = [(a, b) for a in outer for b in a["items"]]\nfor a, b in pairs:\n    await ctx.tool("touch", a=a, b=b)',
            )
        self.call_sites.append({
            "id": site_id, "kind": kind, "line": call.lineno, "col": call.col_offset,
            "tool": literal if kind == "tool" else None,
            "playbook": literal if kind == "subtask" else None,
            "loop_depth": depth, "in_try": in_try,
        })

    def _option(self, kind: str, kw: ast.keyword, options: frozenset[str]) -> None:
        name = kw.arg or ""
        ex = f'await ctx.{kind}(..., _id="step", _timeout={DEFAULT_TIMEOUTS.get(kind) or 60})' \
            if "_timeout" in options else f'await ctx.{kind}(..., _id="step")'
        if name not in ("_id", "_timeout", "_retry"):
            self.issue(
                "v2-option", kw, f"unknown option `{name}` — options are _id, _timeout, _retry",
                "_id: str, _timeout: seconds, _retry: int | {\"attempts\": int, \"backoff\": float}", ex,
            )
            return
        if name not in options:
            self.issue(
                "v2-option", kw, f"ctx.{kind} does not take `{name}`",
                f"options for ctx.{kind}: {', '.join(sorted(options)) or 'none'}", ex,
            )
            return
        v = kw.value
        if name == "_id" and not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
            self.issue("v2-option", kw, "`_id` must be a string literal", '_id="<unique name>"', ex)
        elif name == "_timeout" and not (isinstance(v, ast.Constant) and isinstance(v.value, (int, float))
                                          and not isinstance(v.value, bool)):
            self.issue("v2-option", kw, "`_timeout` must be a number of seconds", "_timeout=<int | float>", ex)
        elif name == "_retry" and not (
            (isinstance(v, ast.Constant) and isinstance(v.value, int) and not isinstance(v.value, bool))
            or isinstance(v, ast.Dict)
        ):
            self.issue(
                "v2-option", kw, "`_retry` must be an int or {\"attempts\": int, \"backoff\": float}",
                "_retry=3  or  _retry={\"attempts\": 3, \"backoff\": 1.5}", ex,
            )

    @staticmethod
    def _signature_text(kind: str) -> str:
        sig = _SIGNATURES[kind]
        names = list(sig["positional"]) + [f"{k}=" for k in sig["keywords"]]
        req = ", ".join(sig["required"]) or "none"
        return f"ctx.{kind}({', '.join(names)}) — required: {req}"

    @staticmethod
    def _signature_example(kind: str) -> str:
        return {
            "llm": 'answer = await ctx.llm(f"Summarize {row[\'title\']}", output={"s": "str"})',
            "agent": 'verdict = await ctx.agent("Is this email urgent?", output={"urgent": "bool"}, tools=["search"])',
            "subtask": 'child = await ctx.subtask("child-playbook", {"url": inputs["url"]}, returns=["count"])',
            "approve": "await ctx.approve(show=summaries)",
            "wait_event": 'payload = await ctx.wait_event("order.paid", filter={"id": order_id}, timeout=3600)',
            "now": "now = await ctx.now()",
            "random": "r = await ctx.random()",
            "log": 'await ctx.log(f"fetched {len(rows)} rows")',
            "tool": 'rows = await ctx.tool("fetch_list", url=inputs["url"])',
        }[kind]

    def _example_awaited(self, call: ast.Call, kind: str) -> str:
        seg = ast.get_source_segment(self.code, call) or f"ctx.{kind}(...)"
        if "\n" in seg:
            seg = f"ctx.{kind}(...)"
        return f"result = await {seg}"

    def _await_ok(self, call: ast.Call) -> bool:
        t = self.t
        p = t.parent_of(call)
        if isinstance(p, ast.Await):
            return True
        if _is_gather_call(p) and t.field_of(call) == "args":
            return True
        if isinstance(p, ast.Starred) and _is_gather_call(t.parent_of(p)):
            return True
        if (isinstance(p, (ast.List, ast.Tuple)) and t.field_of(call) == "elts") \
                or (isinstance(p, (ast.ListComp, ast.GeneratorExp)) and t.field_of(call) == "elt"):
            return self._container_gather_only(p)
        if isinstance(p, ast.Call) and isinstance(p.func, ast.Attribute) and p.func.attr in ("append", "extend") \
                and isinstance(p.func.value, ast.Name) and t.field_of(call) == "args":
            return self._name_gather_only(p.func.value.id, t.scope_of(call))
        return False

    def _container_gather_only(self, cont: ast.AST) -> bool:
        t = self.t
        p = t.parent_of(cont)
        if isinstance(p, ast.Starred) and _is_gather_call(t.parent_of(p)):
            return True
        if isinstance(p, ast.Assign) and len(p.targets) == 1 and isinstance(p.targets[0], ast.Name):
            return self._name_gather_only(p.targets[0].id, t.scope_of(cont))
        return False

    def _name_gather_only(self, name: str, scope: ast.AST) -> bool:
        t = self.t
        loads = [n for n in ast.walk(scope) if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)]
        if not loads:
            return False
        for n in loads:
            p = t.parent_of(n)
            if isinstance(p, ast.Starred) and _is_gather_call(t.parent_of(p)):
                continue
            if isinstance(p, ast.Attribute) and p.attr in ("append", "extend"):
                pp = t.parent_of(p)
                if isinstance(pp, ast.Call) and pp.func is p:
                    continue
            return False
        return True

    def _call_site_id(self, call: ast.Call, kind: str, literal: str | None) -> str:
        for kw in call.keywords:
            if kw.arg == "_id" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
        t = self.t
        p = t.parent_of(call)
        if isinstance(p, ast.Await):
            pp = t.parent_of(p)
            if isinstance(pp, ast.Assign) and len(pp.targets) == 1 and isinstance(pp.targets[0], ast.Name):
                return pp.targets[0].id
            if isinstance(pp, ast.AnnAssign) and isinstance(pp.target, ast.Name):
                return pp.target.id
        if literal is not None and kind in ("tool", "subtask"):
            return literal
        return kind

    # -- R26: ported v1 lints ---------------------------------------------------------------
    def _ported_lints(self) -> None:
        run = self.run_def
        if run is None:
            return
        t = self.t
        # whole tool results bound to a name (for context-economy)
        tool_result_names: set[str] = set()
        for n in ast.walk(run):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                    and isinstance(n.value, ast.Await) and _effect_kind(n.value.value) == "tool":
                tool_result_names.add(n.targets[0].id)
        work: list[ast.Call] = []
        model_calls: list[tuple[ast.Call, str]] = []
        for n in ast.walk(run):
            k = _effect_kind(n)
            if k is None or k not in _WORK_KINDS:
                continue
            if k == "tool" and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == _DELIVERY_TOOL:
                continue
            work.append(n)
            if k in ("llm", "agent"):
                model_calls.append((n, k))
        for call, kind in model_calls:
            prompt_node = call.args[0] if call.args else next(
                (kw.value for kw in call.keywords if kw.arg == "prompt"), None,
            )
            text = _prompt_text(prompt_node)
            if text is None:
                continue
            m = _prompt_markers(text)
            looped = t.loop_depth(call) >= 1
            flagged = self._collection_ref(prompt_node, tool_result_names)
            if kind == "agent" and m["io_verbs"] and not m["judgment"]:
                self.issue(
                    "agent-does-work", call,
                    f"this ctx.agent looks like mechanical work ({', '.join(m['io_verbs'])}) with no judgment",
                    "reserve ctx.agent for DECISIONS; do work with ctx.tool (or a loop of ctx.tool calls)",
                    'page = await ctx.tool("http_request", url=inputs["url"])\n'
                    'verdict = await ctx.agent(f"Is this page relevant? {page[\'text\'][:2000]}", output={"relevant": "bool"})',
                )
            if not looped:
                reasons: list[str] = []
                if m["quantifier"]:
                    reasons.append("a quantifier (each/every/all)")
                if m["sequence"]:
                    reasons.append("a sequence (and-then / numbered steps)")
                if len(m["action_verbs"]) >= 2:
                    reasons.append(f"multiple operations ({', '.join(m['action_verbs'])})")
                if reasons:
                    self.issue(
                        "compound-leaf", call,
                        f"this ctx.{kind} prompt describes " + "; ".join(reasons)
                        + " — that is a hidden loop / multi-step",
                        "loop over the items and do ONE operation per iteration, or split into separate effects",
                        'for r in rows["items"]:\n    s = await ctx.llm(f"Summarize {r[\'title\']}", output={"s": "str"})',
                    )
                if flagged:
                    self.issue(
                        "context-economy", call,
                        f"this ctx.{kind} feeds a whole collection ({flagged}) into one model call — "
                        "that can explode the context window",
                        "loop over the items, summarize ONE per iteration, then collect",
                        'for r in rows["items"]:\n    s = await ctx.llm(f"Summarize {r[\'title\']}", output={"s": "str"})',
                    )
        if len(work) == 1 and model_calls and work[0] is model_calls[0][0]:
            call, kind = model_calls[0]
            prompt_node = call.args[0] if call.args else next(
                (kw.value for kw in call.keywords if kw.arg == "prompt"), None,
            )
            text = _prompt_text(prompt_node)
            if text is not None:
                m = _prompt_markers(text)
                multi_op_io = len(m["action_verbs"]) >= 2 and bool(m["io_verbs"])
                if m["quantifier"] or m["sequence"] or multi_op_io \
                        or self._collection_ref(prompt_node, tool_result_names):
                    self.issue(
                        "monolithic-playbook", call,
                        f"this playbook delegates the whole task to a single ctx.{kind} call — "
                        "that is a prompt wearing a playbook costume",
                        "decompose: fetch (ctx.tool) -> loop(ONE judgment per item) -> reduce (ctx.llm) -> deliver (ctx.tool)",
                        'rows = await ctx.tool("fetch_list", url=inputs["url"])\n'
                        'summaries = [(await ctx.llm(f"Summarize {r[\'title\']}", output={"s": "str"}))["s"] for r in rows["items"]]\n'
                        'await ctx.tool("send_message", to=inputs["owner"], text="\\n".join(summaries))',
                    )

    def _collection_ref(self, prompt_node: ast.AST | None, tool_result_names: set[str]) -> str | None:
        for expr in _interpolations(prompt_node):
            seg = ast.get_source_segment(self.code, expr) or "?"
            if isinstance(expr, ast.Name) and expr.id in tool_result_names:
                return seg
            if isinstance(expr, ast.Subscript) and isinstance(expr.slice, ast.Constant) \
                    and isinstance(expr.slice.value, str):
                key = expr.slice.value
                if isinstance(expr.value, ast.Name) and expr.value.id == "inputs":
                    prop = (self.inputs_props or {}).get(key)
                    if isinstance(prop, dict) and prop.get("type") == "array":
                        return seg
                elif _is_collection_word(key):
                    return seg
            if isinstance(expr, ast.Attribute) and _is_collection_word(expr.attr):
                return seg
        return None


def check(
    code: str, *, name: str, version: int | str, inputs_schema: dict | None = None,
    tool_names: set[str] | None = None, features: Any = DEFAULT_FEATURES,
) -> CheckResult:
    """Every issue at once (docs/v2.md §7-§8). Pure: no I/O, nothing executed."""
    return _Checker(
        code or "", name=name, version=version, inputs_schema=inputs_schema,
        tool_names=tool_names, features=features,
    ).run()


__all__ = [
    "CheckIssue", "CheckResult", "Rule", "RULES", "IMPORT_WHITELIST", "BANNED_CALLS",
    "BANNED_ATTRS", "MAX_CALL_SITES", "check", "resolve_format", "sniff_format", "stable_filename",
]
