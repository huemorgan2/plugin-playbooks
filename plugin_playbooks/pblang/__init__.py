"""pblang — the restricted-Python authoring layer for playbooks.

The agent writes playbooks as Python source; the source is AST-parsed
(never executed) and compiled to the ``PlaybookDef`` IR, which stays the
stored/validated/executed format. ``codegen`` renders the IR back to Python
with variable names equal to step ids, so ``compile(codegen(ir)) == ir``.
"""

from .compiler import CompileIssue, PlaybookCompileError, compile_playbook
from .codegen import generate_code


def defs_equal(a, b) -> bool:
    """Semantic equality of two PlaybookDefs (the round-trip check)."""
    def dump(p):
        return p.model_dump(mode="json", exclude_none=True, by_alias=True)
    return dump(a) == dump(b)


__all__ = [
    "CompileIssue",
    "PlaybookCompileError",
    "compile_playbook",
    "generate_code",
    "defs_equal",
]
