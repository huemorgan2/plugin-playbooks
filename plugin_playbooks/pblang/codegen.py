"""pblang.codegen — PlaybookDef IR → restricted-Python source.

The inverse of compiler.py. Variable names are the step ids, so the
round-trip ``compile_playbook(generate_code(ir))`` preserves ids exactly.
Everything is emitted as plain literals (strings stay the verbatim Jinja
the IR stores) — codegen never tries to reverse-engineer a Jinja
expression back into Python syntax.
"""

from __future__ import annotations

import keyword
from typing import Any

from ..definition import PlaybookDef, StepDef, StepKind

# Names a step id cannot take as a bare variable (they'd shadow the language).
RESERVED_NAMES = {
    "playbook", "trigger",
    "tool", "llm", "agent", "if_", "loop", "parallel", "approve",
    "wait_event", "subtask", "state", "halt", "code",
    "set_", "append", "extend", "push_back", "push_front", "pop_back",
    "pop_front", "add_unique", "incr", "decr", "merge", "delete",
    "inputs", "vars", "steps", "event", "range", "id", "args", "output",
}

_INDENT = "    "


def _is_var_name(s: str) -> bool:
    return s.isidentifier() and not keyword.iskeyword(s) and s not in RESERVED_NAMES


def _fmt_str(s: str) -> str:
    if "\n" in s and "\\" not in s and '"""' not in s and not s.endswith('"'):
        return f'"""{s}"""'
    return repr(s)


def _fmt(value: Any, indent: int = 0) -> str:
    """Format a JSON-ish value as Python literal source."""
    if isinstance(value, str):
        return _fmt_str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, dict):
        inline = "{" + ", ".join(
            f"{_fmt(k)}: {_fmt(v)}" for k, v in value.items()
        ) + "}"
        if len(inline) <= 72 and "\n" not in inline:
            return inline
        pad = _INDENT * (indent + 1)
        items = ",\n".join(
            f"{pad}{_fmt(k)}: {_fmt(v, indent + 1)}" for k, v in value.items()
        )
        return "{\n" + items + ",\n" + _INDENT * indent + "}"
    if isinstance(value, (list, tuple)):
        inline = "[" + ", ".join(_fmt(v) for v in value) + "]"
        if len(inline) <= 72 and "\n" not in inline:
            return inline
        pad = _INDENT * (indent + 1)
        items = ",\n".join(f"{pad}{_fmt(v, indent + 1)}" for v in value)
        return "[\n" + items + ",\n" + _INDENT * indent + "]"
    return repr(value)


def _retry_kwarg(step: StepDef) -> str | None:
    if step.retry.max == 0:
        return None
    if step.retry.backoff_seconds == 1.0:
        return _fmt(step.retry.max)
    return _fmt({"max": step.retry.max,
                 "backoff_seconds": step.retry.backoff_seconds})


def _common_kwargs(step: StepDef) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if step.explanation:
        out.append(("explanation", _fmt(step.explanation)))
    r = _retry_kwarg(step)
    if r is not None:
        out.append(("retry", r))
    if step.on_error.value != "abort":
        out.append(("on_error", _fmt(step.on_error.value)))
    if step.timeout is not None:
        out.append(("timeout", _fmt(step.timeout)))
    return out


def _state_op_call(op) -> str:
    fn = "set_" if op.op == "set" else op.op
    parts = [_fmt(op.var)]
    if op.op in ("pop_back", "pop_front"):
        if op.into is not None:
            parts.append(f"into={_fmt(op.into)}")
    elif op.op == "delete":
        pass
    elif op.value is not None or op.op not in ("incr", "decr"):
        parts.append(_fmt(op.value))
    return f"{fn}({', '.join(parts)})"


class _CodeGen:
    def emit_step_call(self, step: StepDef, indent: int,
                       include_id: bool) -> list[str]:
        """Render one step as a call expression, as indented lines.

        The first line has no indent prefix (the caller places it); nested
        lines are indented relative to ``indent``.
        """
        func, kwargs = self._step_parts(step, indent)
        if include_id:
            kwargs.append(("id", _fmt(step.id)))
        kwargs.extend(_common_kwargs(step))

        pieces: list[str] = []
        multiline = False
        for k, v in kwargs:
            frag = f"{k}={v}" if k else v
            pieces.append(frag)
            if "\n" in frag:
                multiline = True
        inline = f"{func}({', '.join(pieces)})"
        if not multiline and len(inline) + indent * 4 <= 88:
            return [inline]
        pad = _INDENT * (indent + 1)
        lines = [f"{func}("]
        for frag in pieces:
            frag_lines = frag.split("\n")
            lines.append(pad + frag_lines[0])
            lines.extend(frag_lines[1:])
            lines[-1] += ","
        lines.append(_INDENT * indent + ")")
        return lines

    def _step_list(self, steps: list[StepDef], indent: int) -> str:
        """A body/then/else list rendered as a multiline [...] literal.

        Convention (shared with _fmt): the returned string's first line is
        UNPADDED — the caller places it; continuation lines carry their own
        absolute indentation, formatted as if the value sits at ``indent``.
        """
        pad = _INDENT * (indent + 1)
        lines = ["["]
        for s in steps:
            call = self.emit_step_call(s, indent + 1, include_id=True)
            call[0] = pad + call[0]
            call[-1] += ","
            lines.extend(call)
        lines.append(_INDENT * indent + "]")
        return "\n".join(lines)

    def _branch_list(self, branches: list[list[StepDef]], indent: int) -> str:
        pad = _INDENT * (indent + 1)
        lines = ["["]
        for b in branches:
            sub = self._step_list(b, indent + 1).split("\n")
            sub[0] = pad + sub[0]
            sub[-1] += ","
            lines.extend(sub)
        lines.append(_INDENT * indent + "]")
        return "\n".join(lines)

    def _step_parts(
        self, step: StepDef, indent: int,
    ) -> tuple[str, list[tuple[str, str]]]:
        """(func_name, [(kwarg, formatted_value)]) — '' kwarg = positional."""
        k = step.kind
        kw: list[tuple[str, str]] = []
        if k == StepKind.TOOL_CALL:
            kw.append(("", _fmt(step.tool or "")))
            args = step.args or {}
            loose_ok = all(
                key.isidentifier() and not keyword.iskeyword(key)
                and key not in {"id", "args", "explanation", "retry",
                                "on_error", "timeout"}
                for key in args
            )
            if loose_ok:
                for key, v in args.items():
                    kw.append((key, _fmt(v, indent + 1)))
            else:
                kw.append(("args", _fmt(args, indent + 1)))
            return "tool", kw
        if k in (StepKind.LLM_STEP, StepKind.AGENT_STEP):
            kw.append(("", _fmt(step.prompt or "")))
            if step.output_schema is not None:
                kw.append(("output", _fmt(step.output_schema, indent + 1)))
            if k == StepKind.LLM_STEP:
                for f in ("purpose", "model", "system"):
                    v = getattr(step, f)
                    if v is not None:
                        kw.append((f, _fmt(v)))
                return "llm", kw
            if step.tools is not None:
                kw.append(("tools", _fmt(step.tools, indent + 1)))
            return "agent", kw
        if k == StepKind.CONDITION:
            kw.append(("", _fmt(step.when or "")))
            kw.append(("then", self._step_list(step.then or [], indent + 1)))
            if step.else_ is not None:
                kw.append(("else_", self._step_list(step.else_, indent + 1)))
            return "if_", kw
        if k == StepKind.LOOP:
            if step.over is not None:
                kw.append(("over", _fmt(step.over, indent + 1)))
            if step.item_name is not None:
                kw.append(("item_name", _fmt(step.item_name)))
            if step.while_ is not None:
                kw.append(("while_", _fmt(step.while_)))
            if step.until is not None:
                kw.append(("until", _fmt(step.until)))
            if step.max_iterations != 100:
                kw.append(("max_iterations", _fmt(step.max_iterations)))
            if step.concurrency != 1:
                kw.append(("concurrency", _fmt(step.concurrency)))
            kw.append(("body", self._step_list(step.body or [], indent + 1)))
            if step.break_when is not None:
                kw.append(("break_when", _fmt(step.break_when)))
            if step.collect is not None:
                kw.append(("collect", _fmt(step.collect)))
            return "loop", kw
        if k == StepKind.PARALLEL:
            kw.append(("", self._branch_list(step.branches or [], indent + 1)))
            if step.fan_in != "all":
                kw.append(("fan_in", _fmt(step.fan_in)))
            return "parallel", kw
        if k == StepKind.WAIT_FOR_APPROVAL:
            if step.show is not None:
                kw.append(("show", _fmt(step.show, indent + 1)))
            return "approve", kw
        if k == StepKind.WAIT_FOR_EVENT:
            kw.append(("", _fmt(step.event or "")))
            if step.event_filter is not None:
                kw.append(("filter", _fmt(step.event_filter, indent + 1)))
            if step.timeout_seconds is not None:
                kw.append(("timeout_seconds", _fmt(step.timeout_seconds)))
            return "wait_event", kw
        if k == StepKind.SUBTASK:
            kw.append(("", _fmt(step.playbook or "")))
            if step.inputs_map is not None:
                kw.append(("inputs", _fmt(step.inputs_map, indent + 1)))
            if step.returns is not None:
                kw.append(("returns", _fmt(step.returns, indent + 1)))
            return "subtask", kw
        if k == StepKind.STATE:
            for op in step.state or []:
                kw.append(("", _state_op_call(op)))
            return "state", kw
        if k == StepKind.CODE:
            kw.append(("", _fmt(step.source or "")))
            if step.code_inputs is not None:
                kw.append(("inputs", _fmt(step.code_inputs, indent + 1)))
            return "code", kw
        if k == StepKind.HALT:
            if step.when is not None:
                kw.append(("when", _fmt(step.when)))
            if step.value is not None:
                kw.append(("value", _fmt(step.value, indent + 1)))
            return "halt", kw
        raise ValueError(f"Unknown step kind: {k}")


_DEFAULT_INPUTS = {"type": "object", "properties": {}}


def generate_code(pb: PlaybookDef) -> str:
    """Render a PlaybookDef as restricted-Python playbook source."""
    g = _CodeGen()
    lines: list[str] = []

    header: list[tuple[str, str]] = [("name", _fmt(pb.name))]
    if pb.display_name:
        header.append(("display_name", _fmt(pb.display_name)))
    if pb.description:
        header.append(("description", _fmt(pb.description)))
    if pb.explanation:
        header.append(("explanation", _fmt(pb.explanation)))
    if pb.when_to_use:
        header.append(("when_to_use", _fmt(pb.when_to_use)))
    if pb.agent_autonomy.value != "agent_must_confirm":
        header.append(("agent_autonomy", _fmt(pb.agent_autonomy.value)))
    if pb.inputs and pb.inputs != _DEFAULT_INPUTS:
        header.append(("inputs", _fmt(pb.inputs, 1)))
    if pb.triggers:
        trig_lines = ["["]
        for t in pb.triggers:
            parts = [f"event={_fmt(t.event)}"]
            if t.filter:
                parts.append(f"filter={_fmt(t.filter)}")
            if t.map:
                parts.append(f"map={_fmt(t.map)}")
            if t.if_expr is not None:
                parts.append(f"if_={_fmt(t.if_expr)}")
            trig_lines.append(_INDENT * 2 + f"trigger({', '.join(parts)}),")
        trig_lines.append(_INDENT + "]")
        header.append(("triggers", "\n".join(trig_lines)))

    lines.append("playbook(")
    for k, v in header:
        v_lines = v.split("\n")
        lines.append(_INDENT + f"{k}={v_lines[0]}")
        lines.extend(v_lines[1:])
        lines[-1] += ","
    lines.append(")")
    lines.append("")

    for step in pb.steps:
        as_var = _is_var_name(step.id)
        call_lines = g.emit_step_call(step, 0, include_id=not as_var)
        if as_var:
            call_lines[0] = f"{step.id} = {call_lines[0]}"
        lines.extend(call_lines)

    return "\n".join(lines) + "\n"
