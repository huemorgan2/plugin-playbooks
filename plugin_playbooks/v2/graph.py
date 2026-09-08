"""plans/032 phase 10 — the canvas graph of a python playbook, derived from
the code on the server (master §2 Canvas).

`build_graph` parses the source with `ast` and walks the body of
`async def run` into a block tree: one step node per `ctx` call site (the
checker's call-site id, node id `step-<id>`), `if`/`for`/`while` containers,
`try` as an `error_boundary`, `ctx.gather` as a fan-out with its argument
sites, and the code between effects collapsed into `compute` nodes. Every id
is a function of call-site ids, never of line numbers, so an edit that does
not touch a call site keeps every node id.

`trace_rows` maps a run's journal entries onto those nodes
(`step-<call_site_id>` + `occurrence`); `parse_failed_line` reads the
`line <n>:` prefix of the run-level one-liner (docs/v2.md §7) so a pure
compute failure lands on the compute node covering that line
(`node_at_line`).

stdlib `ast` only; nothing from the runner or the checker.
"""

from __future__ import annotations

import ast
import re
from typing import Any

LABEL_MAX = 40

# journal status → UI RunStatus (ui-src/src/playbooks/types.ts)
STATUS_MAP = {
    "in_flight": "running",
    "done": "done",
    "failed": "failed",
    "failed_handled": "done",
    "timed_out_unknown": "failed",
    "parked": "waiting",
}

_FAILED_LINE_RE = re.compile(r"^line (\d+):")


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LABEL_MAX else text[: LABEL_MAX - 1] + "…"


def _is_ctx_call(node: ast.AST) -> str | None:
    """`ctx.<attr>(...)` → attr, else None."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "ctx":
        return f.attr
    return None


class _Builder:
    def __init__(self, code: str, call_sites: list[dict[str, Any]]) -> None:
        self.code = code
        self.lines = code.splitlines()
        self.sites = {(int(c["line"]), int(c["col"])): c for c in call_sites}
        self._container_refs: dict[str, int] = {}

    # -- helpers -----------------------------------------------------------------
    def _src(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.code, node) or ""

    def _container_id(self, kind: str, ref: str) -> str:
        base = f"{kind}-{ref}"
        n = self._container_refs.get(base, 0) + 1
        self._container_refs[base] = n
        return base if n == 1 else f"{base}_{n}"

    def _effects(self, node: ast.AST) -> list[dict[str, Any]]:
        """Effect items inside `node` in execution order (arguments before
        the call): call-site nodes, and gather items carrying their argument
        sites. Nothing is emitted twice."""
        out: list[dict[str, Any]] = []
        self._walk_effects(node, out)
        return out

    def _walk_effects(self, node: ast.AST, out: list[dict[str, Any]]) -> None:
        attr = _is_ctx_call(node)
        if attr == "gather":
            args: list[dict[str, Any]] = []
            for child in ast.iter_child_nodes(node):
                self._walk_effects(child, args)
            out.append(self._gather_item(node, args))
            return
        for child in ast.iter_child_nodes(node):
            self._walk_effects(child, out)
        if attr is not None:
            site = self.sites.get((node.lineno, node.col_offset))
            if site is not None:
                out.append(self._site_item(node, site))

    def _site_item(self, call: ast.Call, site: dict[str, Any]) -> dict[str, Any]:
        sid = str(site["id"])
        return {
            "node": f"step-{sid}",
            "kind": site.get("kind") or "tool",
            "call_site_id": sid,
            "label": sid,
            "sublabel": site.get("tool") or site.get("playbook") or None,
            "line": call.lineno,
            "col": call.col_offset,
            "end_line": call.end_lineno or call.lineno,
            "loop_depth": site.get("loop_depth", 0),
            "in_try": bool(site.get("in_try", False)),
        }

    def _gather_item(self, call: ast.Call, args: list[dict[str, Any]]) -> dict[str, Any]:
        ref = args[0]["call_site_id"] if args and args[0].get("call_site_id") else "gather"
        return {
            "node": self._container_id("gather", ref),
            "kind": "gather",
            "call_site_id": None,
            "label": f"gather ({len(args)})",
            "sublabel": None,
            "line": call.lineno,
            "col": call.col_offset,
            "end_line": call.end_lineno or call.lineno,
            "loop_depth": args[0].get("loop_depth", 0) if args else 0,
            "in_try": bool(args[0].get("in_try", False)) if args else False,
            "args": args,
        }

    def _sites_in(self, node: ast.AST) -> list[dict[str, Any]]:
        """Call sites inside `node` in document order (no side effects)."""
        found = []
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and (n.lineno, n.col_offset) in self.sites:
                found.append(self.sites[(n.lineno, n.col_offset)])
        found.sort(key=lambda c: (int(c["line"]), int(c["col"])))
        return found

    # -- blocks -------------------------------------------------------------------
    def block(self, stmts: list[ast.stmt], block_id: str) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        pending: list[ast.stmt] = []  # statements of the compute run being collected

        def flush(next_node: str | None) -> None:
            if not pending:
                return
            first, last = pending[0], pending[-1]
            line = first.lineno
            end_line = last.end_lineno or last.lineno
            items.append({
                "node": f"compute-{next_node}" if next_node else f"compute-end-{block_id}",
                "kind": "compute",
                "label": _clip(self.lines[line - 1].strip() if 0 < line <= len(self.lines) else "…"),
                "line": line,
                "end_line": end_line,
                "lines": end_line - line + 1,
            })
            pending.clear()

        for stmt in self._expand(stmts):
            new_items = self._stmt_items(stmt) if isinstance(stmt, ast.stmt) else stmt
            if not new_items:
                pending.append(stmt)  # type: ignore[arg-type]
                continue
            flush(new_items[0]["node"])
            items.extend(new_items)
        flush(None)
        return {"id": block_id, "items": items}

    def _stmt_items(self, stmt: ast.stmt) -> list[dict[str, Any]]:
        """Items for one statement: [] when it holds no call site (it joins the
        surrounding compute node), effect nodes for a plain statement, a
        container for an if/for/while/try with a call site inside."""
        if isinstance(stmt, ast.If):
            inner = self._sites_in(stmt)
            if not inner:
                return []
            head = self._effects(stmt.test)
            ref = str(inner[0]["id"])
            node = self._container_id("if", ref)
            then_b = self.block(stmt.body, f"{node}-then")
            else_b = self.block(stmt.orelse, f"{node}-else") if stmt.orelse else None
            return head + [{
                "node": node, "kind": "if", "label": _clip("if " + self._src(stmt.test)),
                "line": stmt.lineno, "end_line": stmt.end_lineno or stmt.lineno,
                "then": then_b, "else": else_b,
            }]
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            inner = self._sites_in(stmt)
            if not inner:
                return []
            kind = "while" if isinstance(stmt, ast.While) else "for"
            head = self._effects(stmt.test if isinstance(stmt, ast.While) else stmt.iter)
            ref = str(inner[0]["id"])
            node = self._container_id(kind, ref)
            if isinstance(stmt, ast.While):
                label = "while " + self._src(stmt.test)
            else:
                label = f"for {self._src(stmt.target)} in {self._src(stmt.iter)}"
            body = self.block(list(stmt.body) + list(stmt.orelse), node)
            return head + [{
                "node": node, "kind": kind, "label": _clip(label),
                "line": stmt.lineno, "end_line": stmt.end_lineno or stmt.lineno, "body": body,
            }]
        if isinstance(stmt, ast.Try) or (hasattr(ast, "TryStar") and isinstance(stmt, ast.TryStar)):
            inner = self._sites_in(stmt)
            if not inner:
                return []
            ref = str(inner[0]["id"])
            node = self._container_id("try", ref)
            body = self.block(list(stmt.body) + list(stmt.orelse), node)
            handlers = []
            for i, h in enumerate(stmt.handlers, 1):
                types = self._src(h.type) if h.type is not None else ""
                handlers.append({
                    "label": _clip(f"except {types}".rstrip()),
                    "line": h.lineno, "end_line": h.end_lineno or h.lineno,
                    "body": self.block(h.body, f"{node}-except-{i}"),
                })
            fin = self.block(stmt.finalbody, f"{node}-finally") if stmt.finalbody else None
            return [{
                "node": node, "kind": "error_boundary", "label": "try",
                "line": stmt.lineno, "end_line": stmt.end_lineno or stmt.lineno,
                "body": body, "handlers": handlers, "finally": fin,
            }]
        return self._effects(stmt)

    def _expand(self, stmts: list[ast.stmt]) -> list[Any]:
        """`with` is transparent: a `with` holding a call site contributes its
        context-expression effects (as a ready item list) followed by its body
        statements, spliced into the enclosing block."""
        out: list[Any] = []
        for stmt in stmts:
            if isinstance(stmt, (ast.With, ast.AsyncWith)) and self._sites_in(stmt):
                head: list[dict[str, Any]] = []
                for it in stmt.items:
                    head += self._effects(it)
                if head:
                    out.append(head)
                out += self._expand(list(stmt.body))
            else:
                out.append(stmt)
        return out


def _children(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Every block (dict with `items`) directly under an item."""
    blocks: list[dict[str, Any]] = []
    for key in ("then", "else", "body", "finally"):
        b = item.get(key)
        if b:
            blocks.append(b)
    for h in item.get("handlers") or []:
        blocks.append(h["body"])
    return blocks


def _flatten(block: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in block["items"]:
        out.append(it)
        for a in it.get("args") or []:
            out.append(a)
        for b in _children(it):
            out += _flatten(b)
    return out


def build_graph(
    code: str, *, name: str, version: int | None, triggers: list[dict[str, Any]] | None,
    call_sites: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """The block tree of `async def run` (see the module docstring)."""
    code = code or ""
    b = _Builder(code, list(call_sites or []))
    run: ast.AsyncFunctionDef | ast.FunctionDef | None = None
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in tree.body:
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "run":
                run = node
                break
    if run is not None:
        root = b.block(list(run.body), "run")
    elif code.strip():
        n = len(b.lines)
        root = {"id": "run", "items": [{
            "node": "compute-end-run", "kind": "compute",
            "label": _clip(next((ln.strip() for ln in b.lines if ln.strip()), "…")),
            "line": 1, "end_line": n, "lines": n,
        }]}
    else:
        root = {"id": "run", "items": []}
    trig = list(triggers or [])
    return {
        "name": name,
        "version": version,
        "format": "python",
        "triggers": trig,
        "node_ids": [f"trigger-{i}" for i in range(len(trig))] + [it["node"] for it in _flatten(root)],
        "root": root,
    }


def node_at_line(graph: dict[str, Any], line: int) -> str | None:
    """The innermost item whose `line..end_line` covers `line`, else None."""

    def visit(block: dict[str, Any]) -> str | None:
        for it in block["items"]:
            hit = visit_item(it)
            if hit:
                return hit
        return None

    def visit_item(it: dict[str, Any]) -> str | None:
        if not (it["line"] <= line <= it.get("end_line", it["line"])):
            return None
        for a in it.get("args") or []:
            hit = visit_item(a)
            if hit:
                return hit
        for blk in _children(it):
            hit = visit(blk)
            if hit:
                return hit
        return it["node"]

    return visit(graph["root"])


def trace_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Journal entries (as `DbJournalStore.read` returns them; entry 0 is
    skipped) → one overlay row per effect occurrence on `step-<id>`."""
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for e in entries:
        if e.get("seq", 0) == 0 or e.get("kind") == "run" or not e.get("id"):
            continue
        sid = str(e["id"])
        counts[sid] = counts.get(sid, 0) + 1
        occ = e.get("occurrence")
        js = e.get("status")
        row = {
            "seq": e.get("seq"),
            "node": f"step-{sid}",
            "call_site_id": sid,
            "occurrence": int(occ) if occ is not None else counts[sid],
            "kind": e.get("kind"),
            "journal_status": js,
            "status": STATUS_MAP.get(js or "", "failed" if js else "pending"),
            "error": e.get("error"),
            "dry": bool(e.get("dry", False)),
            "started_at": e.get("started_at"),
            "ended_at": e.get("ended_at"),
            "ms": e.get("ms"),
            "args": e.get("args"),
            "result": e.get("result"),
        }
        if e.get("parked_on") is not None:
            row["parked_on"] = e["parked_on"]
        rows.append(row)
    return rows


def parse_failed_line(error: str | None) -> int | None:
    """`line 7: rows['items'] → KeyError ... after effect fetch#1` → 7."""
    if not error:
        return None
    m = _FAILED_LINE_RE.match(error)
    return int(m.group(1)) if m else None


__all__ = ["build_graph", "node_at_line", "trace_rows", "parse_failed_line", "STATUS_MAP"]
