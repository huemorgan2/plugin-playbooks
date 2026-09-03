"""plans/023 — zero YAML.

Authoring moved to Python (pblang) and every API surface is JSON. The only
YAML allowed in the package is the refusal shims that steer stale callers
(undeclared `definition_yaml=` / `spec_yaml=` kwargs answered with a
steering error, never a parse) and comments about them. This gate greps the
package so a YAML path can never quietly return.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1] / "plugin_playbooks"

# Lines that may mention yaml: the refusal shims and their comments/tests.
_ALLOWED = re.compile(
    r"YAML (authoring|editing|validation|input|specs).{0,30}removed"
    r"|zero YAML"
    r"|definition_yaml|spec_yaml",
    re.IGNORECASE,
)


def _package_lines():
    for path in sorted(PKG.rglob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            yield path, n, line


def test_no_yaml_imports_or_helpers():
    offenders = [
        f"{p.name}:{n}: {line.strip()}"
        for p, n, line in _package_lines()
        if re.search(r"\bimport yaml\b|parse_yaml|to_yaml|parse_spec_yaml"
                     r"|parse_spec_batch_yaml|safe_load|safe_dump", line)
    ]
    assert not offenders, "\n".join(offenders)


def test_yaml_mentions_are_shims_only():
    offenders = [
        f"{p.name}:{n}: {line.strip()}"
        for p, n, line in _package_lines()
        if "yaml" in line.lower() and not _ALLOWED.search(line)
    ]
    assert not offenders, "\n".join(offenders)


def test_pyproject_has_no_pyyaml():
    text = (PKG.parent / "pyproject.toml").read_text()
    assert "yaml" not in text.lower()


@pytest.mark.asyncio
async def test_yaml_kwargs_steer_never_parse():
    """Every shim answers with a steering error and never yaml-parses."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from plugin_playbooks.agent_tools import build_tools
    from plugin_playbooks.models import Base

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    class _Bus:
        async def emit(self, name, payload):
            pass

    class _Runner:
        _tools = None

    handlers = {td.name: h for td, h in build_tools(sf, _Bus(), _Runner())}
    try:
        for tool, kwargs in [
            ("playbook_propose", {"name": "x", "definition_yaml": "name: x"}),
            ("playbook_validate", {"definition_yaml": "name: x"}),
            ("playbook_edit", {"name": "x", "definition_yaml": "name: x"}),
            ("playbook_spec_add", {"name": "x", "spec_yaml": "inputs: {}"}),
        ]:
            out = json.loads(await handlers[tool](**kwargs))
            assert "removed" in out["error"], (tool, out)
    finally:
        await engine.dispose()
