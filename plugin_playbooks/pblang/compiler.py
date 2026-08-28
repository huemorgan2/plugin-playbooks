"""pblang.compiler — restricted-Python playbook source → PlaybookDef IR.

The source is PARSED with ``ast`` and walked against a whitelist — it is
NEVER executed. Every construct maps 1:1 to a StepDef kind; Python
expressions referencing steps/inputs/loop items compile to the same Jinja
expression strings the YAML layer uses, so the runner is untouched.

Errors are collected all-at-once (like validation.py) and raised as one
``PlaybookCompileError`` whose ``issues`` carry line numbers and fix hints.
"""

from __future__ import annotations

import ast
import copy
import keyword
import re
import textwrap
from dataclasses import dataclass, field
from typing import Any

from ..definition import PlaybookDef


@dataclass
class CompileIssue:
    line: int
    message: str
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"line": self.line, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        return d


class PlaybookCompileError(Exception):
    """Raised with ALL compile issues at once."""

    def __init__(self, issues: list[CompileIssue]):
        self.issues = issues
        lines = "; ".join(
            f"line {i.line}: {i.message}" + (f" ({i.hint})" if i.hint else "")
            for i in issues
        )
        super().__init__(f"Playbook code does not compile — {lines}")


class _ExprError(Exception):
    def __init__(self, node: ast.AST, message: str, hint: str = ""):
        self.node = node
        self.message = message
        self.hint = hint


# Step combinators → StepDef kind.
STEP_FUNCS = {
    "tool": "tool_call",
    "llm": "llm_step",
    "agent": "agent_step",
    "if_": "condition",
    "loop": "loop",
    "parallel": "parallel",
    "approve": "wait_for_approval",
    "wait_event": "wait_for_event",
    "subtask": "subtask",
    "state": "state",
    "halt": "halt",
    # plans/004: jailed Python via plugin-inline-code-run.
    "code": "code",
}

# state() op helpers → StateOp.op names.
STATE_OP_FUNCS = {
    "set_": "set",
    "append": "append",
    "extend": "extend",
    "push_back": "push_back",
    "push_front": "push_front",
    "pop_back": "pop_back",
    "pop_front": "pop_front",
    "add_unique": "add_unique",
    "incr": "incr",
    "decr": "decr",
    "merge": "merge",
    "delete": "delete",
}

# kwargs shared by every step combinator.
COMMON_KWARGS = {"id", "explanation", "retry", "on_error", "timeout"}

# Names an expression may use as a root without being a step id.
BUILTIN_ROOTS = {"inputs", "vars", "steps", "event"}

_ID_RE = re.compile(r"[^a-z0-9_]+")


def _sanitize_id(base: str) -> str:
    s = _ID_RE.sub("_", base.lower()).strip("_") or "step"
    if s[0].isdigit():
        s = "s_" + s
    return s


def _rename_expanded(
    steps: list[dict[str, Any]], mapping: dict[str, str],
) -> list[dict[str, Any]]:
    """plans/004: apply a function-expansion rename to compiled step dicts.

    Renames the names themselves (``id``/``var``/``into``/``collect``
    values) and every ``steps.<name>`` / ``vars.<name>`` reference inside
    string values — compiled Jinja and hand-written Jinja look identical
    at this point, so one string pass covers both. ``source`` (code-step
    Python) is left untouched: its data arrives via the `inputs` dict, and
    rewriting jail code would corrupt it.
    """
    if not mapping:
        return steps
    names = sorted(mapping, key=len, reverse=True)
    ref_re = re.compile(
        r"\b(steps|vars)\.(" + "|".join(re.escape(n) for n in names) + r")\b"
    )

    def fix_str(s: str) -> str:
        return ref_re.sub(lambda m: f"{m.group(1)}.{mapping[m.group(2)]}", s)

    def walk(value: Any, key: str | None = None) -> Any:
        if isinstance(value, str):
            if key in ("id", "var", "into", "collect") and value in mapping:
                return mapping[value]
            if key == "source":
                return value
            return fix_str(value)
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        return value

    return [walk(s) for s in steps]


class _Compiler:
    def __init__(self) -> None:
        self.issues: list[CompileIssue] = []
        self.step_ids: set[str] = set()   # ids defined so far, in source order
        self.item_names: set[str] = set()  # loop item vars currently in scope
        # plans/003 phase 3: names bound by value assignment (x = <expr>).
        # They live in the runner's `vars` namespace; bare `x` in a later
        # expression rewrites to vars.x (checked BEFORE step_ids).
        self.value_names: set[str] = set()
        # plans/004: def functions (macro expansion) + the active call chain
        # (recursion guard).
        self.functions: dict[str, ast.FunctionDef] = {}
        self.expansion_stack: list[str] = []

    # ------------------------------------------------------------------ utils

    def err(self, node: ast.AST | None, message: str, hint: str = "") -> None:
        self.issues.append(
            CompileIssue(getattr(node, "lineno", 0) or 0, message, hint)
        )

    def _register_id(self, node: ast.AST, step_id: str) -> None:
        if step_id in self.step_ids:
            self.err(node, f"Duplicate step id '{step_id}'",
                     "every step id / variable name must be unique")
        if step_id in self.functions:
            self.err(node, f"Step id '{step_id}' collides with a function name")
        self.step_ids.add(step_id)

    def _gen_id(self, base: str) -> str:
        base = _sanitize_id(base)
        candidate, n = base, 1
        while candidate in self.step_ids:
            n += 1
            candidate = f"{base}_{n}"
        return candidate

    # ------------------------------------------------- Python expr → Jinja

    def _transform_expr(self, node: ast.expr) -> ast.expr:
        """Return a rewritten AST that unparses to a Jinja-compatible expr."""
        if isinstance(node, ast.Name):
            if node.id in self.item_names or node.id in BUILTIN_ROOTS:
                return node
            if node.id in self.value_names:
                return ast.copy_location(
                    ast.Attribute(
                        value=ast.Name(id="vars", ctx=ast.Load()),
                        attr=node.id, ctx=ast.Load(),
                    ),
                    node,
                )
            if node.id in self.step_ids:
                return ast.copy_location(
                    ast.Attribute(
                        value=ast.Name(id="steps", ctx=ast.Load()),
                        attr=node.id, ctx=ast.Load(),
                    ),
                    node,
                )
            raise _ExprError(
                node, f"Unknown name '{node.id}' in expression",
                "reference an earlier step by its variable name, "
                "inputs.<field>, vars.<name>, or the loop's item_name; "
                "for Jinja filters pass a raw string like "
                "\"{{ x | length }}\"",
            )
        if isinstance(node, ast.Attribute):
            node.value = self._transform_expr(node.value)
            return node
        if isinstance(node, ast.Subscript):
            node.value = self._transform_expr(node.value)
            node.slice = self._transform_expr(node.slice)
            return node
        if isinstance(node, ast.Constant):
            return node
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div,
                                        ast.FloorDiv, ast.Mod, ast.Pow)):
                raise _ExprError(node, "Unsupported operator in expression")
            node.left = self._transform_expr(node.left)
            node.right = self._transform_expr(node.right)
            return node
        if isinstance(node, ast.BoolOp):
            node.values = [self._transform_expr(v) for v in node.values]
            return node
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.Not, ast.USub)):
                raise _ExprError(node, "Unsupported unary operator")
            node.operand = self._transform_expr(node.operand)
            return node
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if isinstance(op, (ast.Is, ast.IsNot)):
                    raise _ExprError(node, "'is' comparisons are not allowed",
                                     "use == / != instead")
            node.left = self._transform_expr(node.left)
            node.comparators = [self._transform_expr(c) for c in node.comparators]
            return node
        if isinstance(node, ast.IfExp):
            node.test = self._transform_expr(node.test)
            node.body = self._transform_expr(node.body)
            node.orelse = self._transform_expr(node.orelse)
            return node
        if isinstance(node, (ast.List, ast.Tuple)):
            node.elts = [self._transform_expr(e) for e in node.elts]
            return node
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if not isinstance(k, ast.Constant):
                    raise _ExprError(node, "Dict keys in expressions must be literals")
            node.values = [self._transform_expr(v) for v in node.values]
            return node
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "range":
                if node.keywords:
                    raise _ExprError(node, "range() takes only positional args")
                node.args = [self._transform_expr(a) for a in node.args]
                return node
            raise _ExprError(
                node, "Function calls are not allowed in expressions "
                      "(only range())",
                "for Jinja filters/functions pass a raw string like "
                "\"{{ items | selectattr('ok') | list }}\"",
            )
        if isinstance(node, ast.JoinedStr):
            raise _ExprError(
                node, "f-strings are only usable as prompt / arg values, "
                      "not inside conditions",
            )
        raise _ExprError(node, f"Unsupported expression ({type(node).__name__})",
                         "allowed: names, attributes, indexing, literals, "
                         "arithmetic, comparisons, and/or/not, range()")

    def _expr_to_jinja(self, node: ast.expr) -> str:
        rewritten = self._transform_expr(node)
        return ast.unparse(rewritten)

    # ----------------------------------------------------------- value modes

    def _literal(self, node: ast.expr) -> tuple[bool, Any]:
        """(is_literal, value) — pure constants and containers of them."""
        if isinstance(node, ast.Constant):
            return True, node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) \
                and isinstance(node.operand, ast.Constant) \
                and isinstance(node.operand.value, (int, float)):
            return True, -node.operand.value
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for e in node.elts:
                ok, v = self._literal(e)
                if not ok:
                    return False, None
                out.append(v)
            return True, out
        if isinstance(node, ast.Dict):
            out_d: dict[Any, Any] = {}
            for k, v in zip(node.keys, node.values):
                if not isinstance(k, ast.Constant):
                    return False, None
                ok, val = self._literal(v)
                if not ok:
                    return False, None
                out_d[k.value] = val
            return True, out_d
        return False, None

    def template_value(self, node: ast.expr) -> Any:
        """Value in template (render) context: args values, prompts, show.

        Strings pass through verbatim (may carry Jinja); f-strings become
        template strings; other expressions become "{{ ... }}" strings;
        containers recurse.
        """
        try:
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.JoinedStr):
                parts: list[str] = []
                for part in node.values:
                    if isinstance(part, ast.Constant):
                        parts.append(str(part.value))
                    elif isinstance(part, ast.FormattedValue):
                        if part.format_spec is not None or part.conversion != -1:
                            self.err(part, "f-string format specs / conversions "
                                           "are not supported")
                            continue
                        parts.append("{{ " + self._expr_to_jinja(part.value) + " }}")
                return "".join(parts)
            if isinstance(node, (ast.List, ast.Tuple)):
                return [self.template_value(e) for e in node.elts]
            if isinstance(node, ast.Dict):
                out: dict[Any, Any] = {}
                for k, v in zip(node.keys, node.values):
                    if not isinstance(k, ast.Constant):
                        self.err(k or node, "Dict keys must be literals")
                        continue
                    out[k.value] = self.template_value(v)
                return out
            ok, lit = self._literal(node)
            if ok:
                return lit
            return "{{ " + self._expr_to_jinja(node) + " }}"
        except _ExprError as e:
            self.err(e.node, e.message, e.hint)
            return ""

    def eval_value(self, node: ast.expr) -> Any:
        """Value in eval context: when/until/while/break_when/over/collect/
        state-op values/halt value. Literals stay literal; strings pass
        through verbatim (treated as Jinja by the runner); expressions
        become "{{ ... }}" strings.
        """
        ok, lit = self._literal(node)
        if ok:
            return lit
        try:
            return "{{ " + self._expr_to_jinja(node) + " }}"
        except _ExprError as e:
            self.err(e.node, e.message, e.hint)
            return ""

    def literal_value(self, node: ast.expr, what: str) -> Any:
        ok, lit = self._literal(node)
        if not ok:
            self.err(node, f"{what} must be a literal (no expressions)")
            return None
        return lit

    # ------------------------------------------------------------- steps

    def compile_step_call(
        self, call: ast.Call, assigned_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Compile one combinator call into a StepDef dict. Registers the id."""
        if not isinstance(call.func, ast.Name) or call.func.id not in STEP_FUNCS:
            got = ast.unparse(call.func) if isinstance(call, ast.Call) else "?"
            hint = ("allowed: " + ", ".join(sorted(STEP_FUNCS)) + "; for a "
                    "computed value assign a plain expression instead "
                    "(x = inputs.n + 1) and read it back as vars.x")
            if self.functions:
                hint += ("; your functions: "
                         + ", ".join(sorted(self.functions))
                         + " (def before first call)")
            self.err(call, f"Unknown step function '{got}'", hint)
            return None
        func = call.func.id
        kind = STEP_FUNCS[func]

        kwargs = {}
        for kw in call.keywords:
            if kw.arg is None:
                self.err(call, "**kwargs unpacking is not allowed")
                return None
            if kw.arg in kwargs:
                self.err(call, f"Duplicate keyword '{kw.arg}'")
            kwargs[kw.arg] = kw.value

        step: dict[str, Any] = {"kind": kind}

        # --- common options -------------------------------------------------
        explicit_id: str | None = None
        if "id" in kwargs:
            node = kwargs.pop("id")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                explicit_id = node.value
            else:
                self.err(node, "id= must be a string literal")
        if assigned_id is not None and explicit_id is not None \
                and assigned_id != explicit_id:
            self.err(call, f"Step assigned to '{assigned_id}' but has "
                           f"id='{explicit_id}' — use one or the other")
        step_id = assigned_id or explicit_id

        if "explanation" in kwargs:
            v = self.literal_value(kwargs.pop("explanation"), "explanation")
            if isinstance(v, str):
                step["explanation"] = v
        if "retry" in kwargs:
            node = kwargs.pop("retry")
            v = self.literal_value(node, "retry")
            if isinstance(v, int) and not isinstance(v, bool):
                step["retry"] = {"max": v}
            elif isinstance(v, dict):
                step["retry"] = v
            elif v is not None:
                self.err(node, "retry= must be an int (max attempts) or "
                               "{'max': n, 'backoff_seconds': s}")
        if "on_error" in kwargs:
            node = kwargs.pop("on_error")
            v = self.literal_value(node, "on_error")
            if v in ("abort", "continue", "escalate"):
                step["on_error"] = v
            elif v is not None:
                self.err(node, f"on_error= must be 'abort', 'continue' or "
                               f"'escalate', got {v!r}")
        if "timeout" in kwargs:
            v = self.literal_value(kwargs.pop("timeout"), "timeout")
            if isinstance(v, int) and not isinstance(v, bool):
                step["timeout"] = v
            elif v is not None:
                self.err(call, "timeout= must be an int (seconds)")

        # --- per-kind -------------------------------------------------------
        builder = getattr(self, f"_build_{func.rstrip('_')}")
        default_id_base = builder(call, call.args, kwargs, step)

        for name, node in kwargs.items():
            self.err(node, f"Unknown keyword '{name}' for {func}()")

        if step_id is None:
            step_id = self._gen_id(default_id_base or func.rstrip("_"))
            self._register_id(call, step_id)
        else:
            self._register_id(call, step_id)
        step["id"] = step_id
        return step

    def is_value_rhs(self, node: ast.expr) -> bool:
        """plans/003 phase 3: an Assign RHS that is a VALUE, not a step.
        Any non-call expression, plus range(...). Every other call keeps the
        step-call path so a typo'd combinator still errors loudly."""
        if not isinstance(node, ast.Call):
            return True
        return isinstance(node.func, ast.Name) and node.func.id == "range"

    def compile_value_assign(
        self, target_node: ast.AST, target: str, value_node: ast.expr,
    ) -> dict[str, Any]:
        """plans/003 phase 3: `x = <expr>` → a state step setting vars.x.
        Compute once, reuse everywhere — bare `x` in later expressions
        rewrites to vars.x; Jinja strings read {{ vars.x }}."""
        if isinstance(value_node, ast.JoinedStr):
            value = self.template_value(value_node)
        else:
            value = self.eval_value(value_node)
        self._register_id(target_node, target)
        self.value_names.add(target)
        return {
            "kind": "state",
            "id": target,
            "state": [{"op": "set", "var": target, "value": value}],
        }

    # ------------------------------------------------- functions (plans/004)

    def register_function(self, fn: ast.FunctionDef) -> None:
        """Collect a top-level `def` for macro expansion at its call sites."""
        reserved = (set(STEP_FUNCS) | set(STATE_OP_FUNCS) | BUILTIN_ROOTS
                    | {"playbook", "trigger", "range", "code"})
        if fn.name in reserved:
            self.err(fn, f"Function name '{fn.name}' shadows a built-in")
            return
        if fn.name in self.functions:
            self.err(fn, f"Duplicate function '{fn.name}'")
            return
        if fn.name in self.step_ids:
            self.err(fn, f"Function name '{fn.name}' collides with a step id")
            return
        if fn.decorator_list:
            self.err(fn, "Decorators are not allowed on playbook functions")
        a = fn.args
        if a.defaults or a.kw_defaults or a.vararg or a.kwarg or a.kwonlyargs:
            self.err(fn, "Function parameters must be plain positional names "
                         "(no defaults, *args, **kwargs, or keyword-only)")
            return
        params = [x.arg for x in a.posonlyargs + a.args]
        if len(set(params)) != len(params):
            self.err(fn, "Duplicate parameter name")
            return
        for p in params:
            if p in BUILTIN_ROOTS:
                self.err(fn, f"Parameter '{p}' shadows a built-in name")
                return
        self.functions[fn.name] = fn

    def expand_call(self, call: ast.Call) -> list[dict[str, Any]]:
        """Expand a function call into its steps (inline macro expansion).

        Each call gets a unique prefix (`notify`, `notify_2`, …); every step
        id and value name the body defines becomes `<prefix>__<name>`, and
        references to them — bare Python names AND raw-Jinja `steps.x` /
        `vars.x` strings — are rewritten to match.
        """
        fname = call.func.id  # type: ignore[union-attr]
        fn = self.functions[fname]
        if fname in self.expansion_stack:
            chain = " -> ".join(self.expansion_stack + [fname])
            self.err(call, f"Recursive function call ({chain})",
                     "playbook functions cannot recurse — use loop(...)")
            return []
        if len(self.expansion_stack) >= 8:
            self.err(call, "Function calls nested too deeply (max 8)")
            return []

        # --- bind arguments (compiled in the CALLER's scope, before any
        # local names exist) ------------------------------------------------
        params = [x.arg for x in fn.args.posonlyargs + fn.args.args]
        bound: dict[str, ast.expr] = {}
        if len(call.args) > len(params):
            self.err(call, f"{fname}() takes {len(params)} argument(s), "
                           f"got {len(call.args)}")
        for p, arg in zip(params, call.args):
            bound[p] = arg
        for kw in call.keywords:
            if kw.arg is None:
                self.err(call, "**kwargs unpacking is not allowed")
            elif kw.arg not in params:
                self.err(call, f"{fname}() has no parameter '{kw.arg}'")
            elif kw.arg in bound:
                self.err(call, f"{fname}() got multiple values for '{kw.arg}'")
            else:
                bound[kw.arg] = kw.value
        missing = [p for p in params if p not in bound]
        if missing:
            self.err(call, f"{fname}() missing argument(s): "
                           + ", ".join(missing))
            return []
        param_steps: list[dict[str, Any]] = []
        for p in params:
            node = bound[p]
            value = self.template_value(node) if isinstance(node, ast.JoinedStr) \
                else self.eval_value(node)
            param_steps.append({
                "kind": "state", "id": p,
                "state": [{"op": "set", "var": p, "value": value}],
            })

        # --- compile the body in a sub-compiler seeded with the outer scope
        # (so refs to earlier global steps/vars still resolve). Local names
        # may not shadow existing globals — that errors loudly. -------------
        prefix = self._gen_id(fname)
        self.step_ids.add(prefix)
        sub = _Compiler()
        sub.functions = self.functions
        sub.expansion_stack = self.expansion_stack + [fname]
        sub.item_names = set(self.item_names)
        sub.step_ids = set(self.step_ids)
        sub.value_names = set(self.value_names)
        outer_ids = set(sub.step_ids)
        for p in params:
            sub._register_id(call, p)
            sub.value_names.add(p)
        body_steps = sub.compile_stmt_list(
            copy.deepcopy(fn.body), top_level=False)
        for iss in sub.issues:
            self.issues.append(CompileIssue(
                iss.line, f"in {fname}(): {iss.message}", iss.hint))

        new_ids = sub.step_ids - outer_ids
        mapping = {n: f"{prefix}__{n}" for n in new_ids}
        renamed = _rename_expanded(param_steps + body_steps, mapping)
        for n in sorted(new_ids):
            self._register_id(call, mapping[n])
        return renamed

    def compile_stmt_list(
        self, body: list[ast.stmt], *, top_level: bool,
    ) -> list[dict[str, Any]]:
        """Compile a statement sequence — the module body or a def body."""
        steps: list[dict[str, Any]] = []
        for stmt in body:
            if isinstance(stmt, ast.FunctionDef):
                if top_level:
                    self.register_function(stmt)
                else:
                    self.err(stmt, "Nested function definitions are not "
                                   "allowed")
                continue
            if isinstance(stmt, ast.Assign):
                if len(stmt.targets) != 1 \
                        or not isinstance(stmt.targets[0], ast.Name):
                    self.err(stmt, "Assign each step to a single plain "
                                   "variable")
                    continue
                if isinstance(stmt.value, ast.Call) \
                        and isinstance(stmt.value.func, ast.Name) \
                        and stmt.value.func.id in self.functions:
                    self.err(stmt, f"'{stmt.value.func.id}' is a function — "
                                   "functions have no return value",
                             "call it as its own statement; to pass data out "
                             "set a value inside the function (x = ...) ")
                    continue
                # plans/003 phase 3: `x = <expr>` (non-combinator RHS) is a
                # VALUE assignment — compiles to a state set op; read back as
                # vars.x (bare `x` works in later expressions).
                if self.is_value_rhs(stmt.value):
                    steps.append(self.compile_value_assign(
                        stmt, stmt.targets[0].id, stmt.value,
                    ))
                    continue
                s = self.compile_step_call(
                    stmt.value, assigned_id=stmt.targets[0].id)
                if s:
                    steps.append(s)
            elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                fn_node = stmt.value.func
                if isinstance(fn_node, ast.Name) and fn_node.id == "playbook":
                    self.err(stmt, "playbook() must be the first statement "
                                   "(once)")
                    continue
                if isinstance(fn_node, ast.Name) \
                        and fn_node.id in self.functions:
                    steps.extend(self.expand_call(stmt.value))
                    continue
                s = self.compile_step_call(stmt.value)
                if s:
                    steps.append(s)
            elif isinstance(stmt, (ast.For, ast.While)):
                self.err(stmt, "Python for/while loops are not allowed",
                         "use loop(over=..., body=[...]) — it runs "
                         "server-side with retries and visibility")
            elif isinstance(stmt, ast.If):
                self.err(stmt, "Python if statements are not allowed",
                         "use if_(cond, then=[...], else_=[...])")
            elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self.err(stmt, "Imports are not allowed",
                         "the combinators (tool, llm, agent, if_, loop, ...) "
                         "are built in; inside code('''...''') bodies imports "
                         "ARE allowed")
            elif isinstance(stmt, ast.Return):
                self.err(stmt, "return is not allowed — functions are "
                               "procedures",
                         "set a value instead (x = <expr>); after the call "
                         "the caller reads vars.<call_id>__x")
            elif isinstance(stmt, (ast.AsyncFunctionDef, ast.ClassDef)):
                self.err(stmt, "Class / async function definitions are not "
                               "allowed")
            else:
                self.err(stmt, f"Unsupported statement "
                               f"({type(stmt).__name__})",
                         "a playbook is a sequence of step calls, value "
                         "assignments, def functions, and function calls")
        return steps

    def compile_step_list(self, node: ast.expr, what: str) -> list[dict[str, Any]]:
        """A list of step calls (then/else_/body/branch)."""
        if not isinstance(node, ast.List):
            self.err(node, f"{what} must be a list of step calls, e.g. "
                           f"{what}=[tool(...), llm(...)]")
            return []
        steps: list[dict[str, Any]] = []
        for el in node.elts:
            assigned = None
            call = el
            if isinstance(el, ast.NamedExpr):  # (y := tool(...))
                if isinstance(el.target, ast.Name):
                    assigned = el.target.id
                call = el.value
                # plans/003 phase 3: (x := <expr>) — value assignment in a
                # nested list (loop body / then / else_ / branch).
                if assigned is not None and self.is_value_rhs(call):
                    steps.append(
                        self.compile_value_assign(el, assigned, call)
                    )
                    continue
            if not isinstance(call, ast.Call):
                self.err(el, f"Each element of {what} must be a step call",
                         "to bind a computed value here use a walrus: "
                         "(x := <expression>)")
                continue
            # plans/004: function calls expand inline inside nested lists too.
            if isinstance(call.func, ast.Name) and call.func.id in self.functions:
                if assigned is not None:
                    self.err(el, f"'{call.func.id}' is a function — functions "
                                 "have no return value",
                             "call it as its own element; to pass data out "
                             "set a value inside the function (x = ...)")
                    continue
                steps.extend(self.expand_call(call))
                continue
            s = self.compile_step_call(call, assigned_id=assigned)
            if s:
                steps.append(s)
        return steps

    # --- builders. Each consumes kwargs it knows and returns a default-id base.

    def _build_tool(self, call, args, kwargs, step) -> str:
        tool_name = ""
        if args:
            node = args[0]
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                tool_name = node.value
            else:
                self.err(node, "tool() first argument must be the tool name "
                               "as a string literal")
            for extra in args[1:]:
                self.err(extra, "tool() takes one positional argument "
                                "(the tool name); pass args as keywords")
        else:
            self.err(call, "tool() requires the tool name as first argument")
        step["tool"] = tool_name

        if "args" in kwargs:
            node = kwargs.pop("args")
            loose = [k for k in kwargs if k not in COMMON_KWARGS]
            if loose:
                self.err(node, "Use either args={...} or loose keyword args, "
                               "not both")
            step["args"] = self.template_value(node) if isinstance(node, ast.Dict) \
                else self.literal_value(node, "args") or {}
        else:
            tool_args: dict[str, Any] = {}
            for k in list(kwargs):
                tool_args[k] = self.template_value(kwargs.pop(k))
            step["args"] = tool_args
        # default id from the tool's last name segment
        return tool_name.split("__")[-1] if tool_name else "tool"

    def _build_code(self, call, args, kwargs, step) -> str:
        """plans/004: code('...', inputs={...}) — jailed Python, JSON in/out."""
        source = ""
        if args:
            node = args[0]
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                source = node.value
            else:
                self.err(node, "code() first argument must be the Python "
                               "source as a string literal",
                         "use a triple-quoted string; the body runs jailed, "
                         "sees `inputs` as a dict, and must return a "
                         "JSON-serializable value")
            for extra in args[1:]:
                self.err(extra, "code() takes one positional argument "
                                "(the source); pass data via inputs={...}")
        else:
            self.err(call, "code() requires the Python source as first "
                           "argument")
        step["source"] = source
        if source:
            body = textwrap.indent(
                textwrap.dedent(source).strip("\n") or "pass", "    ")
            try:
                ast.parse("def __pb_main__(inputs):\n" + body)
            except SyntaxError as e:
                self.err(call, "code() source has a Python syntax error: "
                               f"{e.msg} (source line {max((e.lineno or 1) - 1, 1)})")
        if "inputs" in kwargs:
            node = kwargs.pop("inputs")
            if isinstance(node, ast.Dict):
                step["code_inputs"] = self.template_value(node)
            else:
                self.err(node, "inputs= must be a dict literal, e.g. "
                               "inputs={'raw': inputs.phone}")
        return "code"

    def _prompt_common(self, call, args, kwargs, step) -> None:
        if args:
            step["prompt"] = self.template_value(args[0])
            for extra in args[1:]:
                self.err(extra, "only one positional argument (the prompt) "
                                "is allowed")
        elif "prompt" in kwargs:
            step["prompt"] = self.template_value(kwargs.pop("prompt"))
        else:
            self.err(call, "a prompt is required (first argument)")
        if not isinstance(step.get("prompt", ""), str):
            self.err(call, "the prompt must be a string")
            step["prompt"] = ""
        if "output" in kwargs:
            v = self.literal_value(kwargs.pop("output"), "output")
            if isinstance(v, dict):
                step["output_schema"] = v
            elif v is not None:
                self.err(call, "output= must be a dict schema")

    def _build_llm(self, call, args, kwargs, step) -> str:
        self._prompt_common(call, args, kwargs, step)
        for k in ("purpose", "model", "system"):
            if k in kwargs:
                v = self.literal_value(kwargs.pop(k), k)
                if isinstance(v, str):
                    step[k] = v
        return "llm"

    def _build_agent(self, call, args, kwargs, step) -> str:
        self._prompt_common(call, args, kwargs, step)
        if "tools" in kwargs:
            v = self.literal_value(kwargs.pop("tools"), "tools")
            if isinstance(v, list) and all(isinstance(t, str) for t in v):
                step["tools"] = v
            elif v is not None:
                self.err(call, "tools= must be a list of tool-name strings")
        return "agent"

    def _build_if(self, call, args, kwargs, step) -> str:
        if args:
            step["when"] = self.eval_value(args[0])
        elif "when" in kwargs:
            step["when"] = self.eval_value(kwargs.pop("when"))
        else:
            self.err(call, "if_() requires a condition as first argument")
        if not isinstance(step.get("when", ""), str):
            self.err(call, "the condition must be an expression or a "
                           "Jinja string")
            step["when"] = ""
        if "then" in kwargs:
            step["then"] = self.compile_step_list(kwargs.pop("then"), "then")
        else:
            self.err(call, "if_() requires then=[...]")
        if "else_" in kwargs:
            step["else"] = self.compile_step_list(kwargs.pop("else_"), "else_")
        return "cond"

    def _build_loop(self, call, args, kwargs, step) -> str:
        for extra in args:
            self.err(extra, "loop() takes keyword arguments only")
        # scalars first, body afterwards (body steps may be referenced by
        # collect/break_when which are compiled after the body).
        if "over" in kwargs:
            step["over"] = self.eval_value(kwargs.pop("over"))
        if "item_name" in kwargs:
            v = self.literal_value(kwargs.pop("item_name"), "item_name")
            if isinstance(v, str):
                step["item_name"] = v
        for k, key in (("until", "until"), ("while_", "while")):
            if k in kwargs:
                v = self.eval_value(kwargs.pop(k))
                if isinstance(v, str):
                    step[key] = v
                else:
                    self.err(call, f"{k}= must be an expression or Jinja string")
        for k in ("max_iterations", "concurrency"):
            if k in kwargs:
                v = self.literal_value(kwargs.pop(k), k)
                if isinstance(v, int) and not isinstance(v, bool):
                    step[k] = v
                elif v is not None:
                    self.err(call, f"{k}= must be an int")

        pushed = []
        item = step.get("item_name")
        if item:
            for nm in (item, f"{item}_index"):
                if nm not in self.item_names:
                    self.item_names.add(nm)
                    pushed.append(nm)
        try:
            if "body" in kwargs:
                step["body"] = self.compile_step_list(kwargs.pop("body"), "body")
            else:
                self.err(call, "loop() requires body=[...]")
            for k in ("break_when", "collect"):
                if k in kwargs:
                    v = self.eval_value(kwargs.pop(k))
                    if isinstance(v, str):
                        step[k] = v
                    else:
                        self.err(call, f"{k}= must be an expression or "
                                       f"Jinja string")
        finally:
            for nm in pushed:
                self.item_names.discard(nm)
        return "loop"

    def _build_parallel(self, call, args, kwargs, step) -> str:
        branches_node = args[0] if args else kwargs.pop("branches", None)
        for extra in args[1:]:
            self.err(extra, "parallel() takes one positional argument "
                            "(the list of branches)")
        if branches_node is None:
            self.err(call, "parallel() requires a list of branches: "
                           "parallel([[...], [...]])")
            step["branches"] = []
        elif not isinstance(branches_node, ast.List):
            self.err(branches_node, "branches must be a list of lists of "
                                    "step calls")
            step["branches"] = []
        else:
            step["branches"] = [
                self.compile_step_list(b, "branch") for b in branches_node.elts
            ]
        if "fan_in" in kwargs:
            v = self.literal_value(kwargs.pop("fan_in"), "fan_in")
            if isinstance(v, str):
                step["fan_in"] = v
        return "parallel"

    def _build_approve(self, call, args, kwargs, step) -> str:
        for extra in args:
            self.err(extra, "approve() takes keyword arguments only")
        if "show" in kwargs:
            node = kwargs.pop("show")
            v = self.template_value(node)
            if isinstance(v, list) and all(isinstance(s, str) for s in v):
                step["show"] = v
            else:
                self.err(node, "show= must be a list of template strings / "
                               "step references")
        return "approve"

    def _build_wait_event(self, call, args, kwargs, step) -> str:
        if args:
            v = self.literal_value(args[0], "event")
            if isinstance(v, str):
                step["event"] = v
            for extra in args[1:]:
                self.err(extra, "wait_event() takes one positional argument")
        elif "event" in kwargs:
            v = self.literal_value(kwargs.pop("event"), "event")
            if isinstance(v, str):
                step["event"] = v
        else:
            self.err(call, "wait_event() requires the event name")
        if "filter" in kwargs:
            v = self.template_value(kwargs.pop("filter"))
            if isinstance(v, dict):
                step["event_filter"] = v
        if "timeout_seconds" in kwargs:
            v = self.literal_value(kwargs.pop("timeout_seconds"), "timeout_seconds")
            if isinstance(v, int) and not isinstance(v, bool):
                step["timeout_seconds"] = v
        return "wait"

    def _build_subtask(self, call, args, kwargs, step) -> str:
        target = ""
        if args:
            v = self.literal_value(args[0], "playbook name")
            if isinstance(v, str):
                target = v
            for extra in args[1:]:
                self.err(extra, "subtask() takes one positional argument "
                                "(the playbook name)")
        elif "playbook" in kwargs:
            v = self.literal_value(kwargs.pop("playbook"), "playbook")
            if isinstance(v, str):
                target = v
        else:
            self.err(call, "subtask() requires the target playbook name")
        step["playbook"] = target
        if "inputs" in kwargs:
            node = kwargs.pop("inputs")
            v = self.template_value(node)
            if isinstance(v, dict) and all(isinstance(x, str) for x in v.values()):
                step["inputs_map"] = v
            else:
                self.err(node, "inputs= must be a dict of template strings / "
                               "expressions")
        if "returns" in kwargs:
            node = kwargs.pop("returns")
            v = self.template_value(node)
            if isinstance(v, dict) and all(isinstance(x, str) for x in v.values()):
                step["returns"] = v
            else:
                self.err(node, "returns= must be a dict of template strings / "
                               "expressions")
        return _sanitize_id(target) if target else "subtask"

    def _build_state(self, call, args, kwargs, step) -> str:
        ops: list[dict[str, Any]] = []
        if not args:
            self.err(call, "state() requires at least one op, e.g. "
                           "state(set_('frontier', '[ inputs.start ]'))")
        for node in args:
            op = self._compile_state_op(node)
            if op:
                ops.append(op)
        step["state"] = ops
        return "state"

    def _compile_state_op(self, node: ast.expr) -> dict[str, Any] | None:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in STATE_OP_FUNCS):
            self.err(node, "state() arguments must be op calls",
                     "allowed: " + ", ".join(sorted(STATE_OP_FUNCS)))
            return None
        fn = node.func.id
        op: dict[str, Any] = {"op": STATE_OP_FUNCS[fn]}
        pos = list(node.args)
        kw = {k.arg: k.value for k in node.keywords}
        var_node = pos.pop(0) if pos else kw.pop("var", None)
        if var_node is None or not (isinstance(var_node, ast.Constant)
                                    and isinstance(var_node.value, str)):
            self.err(node, f"{fn}() requires the variable name as a "
                           f"string literal first argument")
            return None
        op["var"] = var_node.value
        if fn in ("pop_back", "pop_front"):
            into = pos.pop(0) if pos else kw.pop("into", None)
            if into is not None:
                if isinstance(into, ast.Constant) and isinstance(into.value, str):
                    op["into"] = into.value
                else:
                    self.err(into, "into= must be a string literal")
        elif fn != "delete":
            value = pos.pop(0) if pos else kw.pop("value", None)
            if value is not None:
                op["value"] = self.eval_value(value)
            elif fn not in ("incr", "decr"):
                self.err(node, f"{fn}() requires a value")
        for extra in pos:
            self.err(extra, f"too many arguments for {fn}()")
        for name, v in kw.items():
            self.err(v, f"Unknown keyword '{name}' for {fn}()")
        return op

    def _build_halt(self, call, args, kwargs, step) -> str:
        if args:
            step["when"] = self.eval_value(args[0])
            for extra in args[1:]:
                self.err(extra, "halt() takes keyword arguments only "
                                "(optionally a first when= condition)")
        elif "when" in kwargs:
            step["when"] = self.eval_value(kwargs.pop("when"))
        if "value" in kwargs:
            step["value"] = self.eval_value(kwargs.pop("value"))
        return "halt"

    # ------------------------------------------------------------- header

    def compile_header(self, call: ast.Call) -> dict[str, Any]:
        header: dict[str, Any] = {}
        for extra in call.args:
            self.err(extra, "playbook() takes keyword arguments only")
        for kw in call.keywords:
            name, node = kw.arg, kw.value
            if name is None:
                self.err(call, "**kwargs unpacking is not allowed")
                continue
            if name in ("name", "display_name", "description", "explanation",
                        "when_to_use", "agent_autonomy"):
                v = self.literal_value(node, name)
                if isinstance(v, str):
                    header[name] = v
            elif name == "inputs":
                v = self.literal_value(node, "inputs")
                if isinstance(v, dict):
                    header["inputs"] = v
            elif name == "triggers":
                header["triggers"] = self._compile_triggers(node)
            else:
                self.err(node, f"Unknown keyword '{name}' for playbook()")
        if "name" not in header:
            self.err(call, "playbook() requires name=...")
            header["name"] = "unnamed"
        return header

    def _compile_triggers(self, node: ast.expr) -> list[dict[str, Any]]:
        if not isinstance(node, ast.List):
            self.err(node, "triggers= must be a list of trigger(...) calls")
            return []
        out: list[dict[str, Any]] = []
        for el in node.elts:
            if not (isinstance(el, ast.Call) and isinstance(el.func, ast.Name)
                    and el.func.id == "trigger"):
                self.err(el, "each trigger must be a trigger(event=..., "
                             "filter=..., map=..., if_=...) call")
                continue
            t: dict[str, Any] = {}
            for kw in el.keywords:
                name, v = kw.arg, kw.value
                if name == "event":
                    lv = self.literal_value(v, "event")
                    if isinstance(lv, str):
                        t["event"] = lv
                elif name == "filter":
                    lv = self.literal_value(v, "filter")
                    if isinstance(lv, dict):
                        t["filter"] = lv
                elif name == "map":
                    mv = self.template_value(v)
                    if isinstance(mv, dict):
                        t["map"] = mv
                elif name == "if_":
                    t["if"] = self.eval_value(v)
                else:
                    self.err(v, f"Unknown keyword '{name}' for trigger()")
            if "event" not in t:
                self.err(el, "trigger() requires event=...")
                continue
            out.append(t)
        return out


def compile_playbook(source: str, *, name: str | None = None) -> PlaybookDef:
    """Compile restricted-Python playbook source to a PlaybookDef.

    ``name`` (if given) overrides the name declared in the code — the tool
    layer passes the playbook's canonical name so code can never rename.
    """
    c = _Compiler()
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise PlaybookCompileError([CompileIssue(
            e.lineno or 0, f"Python syntax error: {e.msg}",
            "the code is parsed, never executed — plain statements only",
        )]) from e

    body = list(tree.body)
    # allow (and ignore) a module docstring
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body.pop(0)

    header: dict[str, Any] = {"name": name or "unnamed"}
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Call) \
            and isinstance(body[0].value.func, ast.Name) \
            and body[0].value.func.id == "playbook":
        header = c.compile_header(body[0].value)
        body.pop(0)
    else:
        c.err(body[0] if body else None,
              "The first statement must be the playbook(...) declaration",
              "start with playbook(name=..., description=..., inputs=..., "
              "triggers=[...])")

    # plans/004: the statement walk lives on the compiler so def bodies
    # reuse it. def must appear before its first call (single pass).
    steps = c.compile_stmt_list(body, top_level=True)

    if name:
        header["name"] = name
    if c.issues:
        raise PlaybookCompileError(c.issues)

    data = dict(header)
    data["steps"] = steps
    try:
        return PlaybookDef.model_validate(data)
    except Exception as e:  # pydantic ValidationError → one compile issue
        raise PlaybookCompileError([CompileIssue(
            0, f"Definition invalid after compile: {e}", "",
        )]) from e
