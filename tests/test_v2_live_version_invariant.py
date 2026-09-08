"""plans/032 phase 08 (Step 11) — the live_version-writer invariant.

Every write of `Playbook.live_version` goes through a gated publish or an
owner REST surface (master §2 "Lifecycle"). This test walks every module
under `plugin_playbooks/` with `ast` and lists each writer as
`<path>::<enclosing function>`; anything outside the whitelist below is red
with the message "new live_version writer <path>::<fn> — every live write
goes through a gated publish".

Writers the scan recognises:
- `Assign` / `AnnAssign` / `AugAssign` whose target is `<x>.live_version`;
- `keyword(arg="live_version")` in ANY call (constructors, `.values(...)`,
  `.update(...)`);
- the string key `"live_version"` in a dict literal passed to a `values` /
  `update` call, and `<x>["live_version"] = ...` subscript stores.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "plugin_playbooks"

# Whitelist BY SYMBOL (path relative to plugin_playbooks/, enclosing
# function; "<module>" for module/class level). Re-anchored at phase 08
# HEAD — see the phase plan "live_version-writer invariant".
WHITELIST: frozenset[tuple[str, str]] = frozenset({
    # the promote helper (publish/rollback apply a version row to live)
    ("agent_tools.py", "_apply_version_to_live"),
    # transient shim objects never added to a session: the build_tools
    # closure (NOT collapsed in phase 08) and its module-level twin that
    # plugin/06 hoisted for `runner._resume_run` (versioning.py:61)
    ("agent_tools.py", "_shim_playbook"),
    ("versioning.py", "shim_playbook"),
    # the owner REST writers
    ("routes.py", "create_playbook"),
    ("routes.py", "update_playbook"),
    ("routes.py", "_apply_row_to_live"),
    ("routes.py", "put_manifest"),
    ("routes.py", "promote_draft"),
    # the load-time backfill
    ("__init__.py", "backfill_live_version"),
})

# Direct writers the master's whitelist OMITS and a P0 plan owns: the scan
# stays green while the repro pin
# (tests/test_repro_fixplaybooks_lifecycle.py::test_manifest_set_does_not_flip_live)
# stays red. When that plan lands, this set empties (the test below insists
# every entry here is still present, so a landed fix is noticed).
KNOWN_SIDE_DOORS: dict[tuple[str, str], str] = {
    # luna-fixer plans/2026-09-06-manifest-set-live-bypass (Risks 2)
    ("agent_tools.py", "_manifest_set"): "2026-09-06-manifest-set-live-bypass",
}

# the column definition (models.py `Playbook.live_version`) is a class-level
# AnnAssign on a bare Name — not an attribute store — so the scanner never
# lists it; it is named here for completeness of the whitelist only.
COLUMN_DEFINITION = ("models.py", "Playbook")


class _Scanner(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = []
        self.hits: list[tuple[str, str, int]] = []

    # -- scope tracking -----------------------------------------------------
    def _enter(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def _record(self, lineno: int) -> None:
        fn = self.stack[-1] if self.stack else "<module>"
        self.hits.append((self.rel, fn, lineno))

    # -- writers ------------------------------------------------------------
    def _target_writes(self, target) -> bool:
        if isinstance(target, ast.Attribute) and target.attr == "live_version":
            return True
        if isinstance(target, ast.Subscript):
            key = target.slice
            if isinstance(key, ast.Constant) and key.value == "live_version":
                return True
        if isinstance(target, (ast.Tuple, ast.List)):
            return any(self._target_writes(t) for t in target.elts)
        return False

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(self._target_writes(t) for t in node.targets):
            self._record(node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._target_writes(node.target):
            self._record(node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if self._target_writes(node.target):
            self._record(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if any(kw.arg == "live_version" for kw in node.keywords):
            self._record(node.lineno)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("values", "update"):
            for arg in [*node.args, *(kw.value for kw in node.keywords if kw.arg is None)]:
                if isinstance(arg, ast.Dict) and any(
                    isinstance(k, ast.Constant) and k.value == "live_version" for k in arg.keys
                ):
                    self._record(node.lineno)
                    break
        self.generic_visit(node)


def scan(root: Path) -> list[tuple[str, str, int]]:
    """Every live_version writer under `root` as (relpath, function, line)."""
    hits: list[tuple[str, str, int]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        s = _Scanner(rel)
        s.visit(tree)
        hits.extend(s.hits)
    return hits


def assert_no_unlisted_writer(
    root: Path,
    whitelist: frozenset[tuple[str, str]] = WHITELIST,
    side_doors: dict[tuple[str, str], str] = KNOWN_SIDE_DOORS,
) -> list[tuple[str, str, int]]:
    hits = scan(root)
    unlisted = [
        (rel, fn, line) for rel, fn, line in hits
        if (rel, fn) not in whitelist and (rel, fn) not in side_doors
    ]
    assert not unlisted, "\n".join(
        f"new live_version writer {rel}::{fn} (line {line}) — every live write "
        "goes through a gated publish"
        for rel, fn, line in unlisted
    )
    return hits


# ------------------------------------------------------------------ 1
def test_no_unlisted_live_version_writer():
    hits = assert_no_unlisted_writer(PACKAGE)
    found = {(rel, fn) for rel, fn, _ in hits}
    # every whitelisted symbol still writes (a stale entry would silently
    # widen the whitelist when a symbol is renamed)
    stale = sorted(WHITELIST - found)
    assert not stale, f"whitelisted symbols no longer write live_version: {stale}"
    # a landed P0 fix must remove its side-door entry here
    for door, plan in KNOWN_SIDE_DOORS.items():
        assert door in found, (
            f"side door {door[0]}::{door[1]} no longer writes live_version — "
            f"plan {plan} landed; drop it from KNOWN_SIDE_DOORS"
        )
    # `_propose` writes no live_version (plugin/04 removed both writes)
    assert ("agent_tools.py", "_propose") not in found
    # the column definition is a class-level annotation, never a store
    assert COLUMN_DEFINITION not in found
    assert found - WHITELIST == set(KNOWN_SIDE_DOORS)


# ------------------------------------------------------------------ 2
def test_scanner_flags_a_new_writer(tmp_path):
    (tmp_path / "sneaky.py").write_text(
        "def sneak(p):\n"
        "    p.live_version = 3\n",
        encoding="utf-8",
    )
    (tmp_path / "ctor.py").write_text(
        "from models import Playbook\n"
        "async def make():\n"
        "    return Playbook(name='x', live_version=2)\n",
        encoding="utf-8",
    )
    (tmp_path / "bulk.py").write_text(
        "async def bulk(session, stmt):\n"
        "    await session.execute(stmt.values({'live_version': 1}))\n"
        "    d = {}\n"
        "    d['live_version'] = 4\n",
        encoding="utf-8",
    )
    hits = {(rel, fn) for rel, fn, _ in scan(tmp_path)}
    assert hits == {("sneaky.py", "sneak"), ("ctor.py", "make"), ("bulk.py", "bulk")}
    assert len(scan(tmp_path)) == 4  # both bulk.py writes counted
    with pytest.raises(AssertionError) as exc:
        assert_no_unlisted_writer(tmp_path)
    msg = str(exc.value)
    assert "new live_version writer sneaky.py::sneak" in msg
    assert "new live_version writer ctor.py::make" in msg
    assert "new live_version writer bulk.py::bulk" in msg
    assert "every live write goes through a gated publish" in msg
    # whitelisting the fixture symbols makes the same scan pass
    ok = frozenset({("sneaky.py", "sneak"), ("ctor.py", "make"), ("bulk.py", "bulk")})
    assert_no_unlisted_writer(tmp_path, whitelist=ok, side_doors={})
