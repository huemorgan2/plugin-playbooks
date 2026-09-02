"""0.30.0 (plans/018 phase 3) — luna-plugin.toml can no longer drift.

The toml froze at the 0.26 extraction (12 tools / 9 tables declared vs 25 /
10 real, a policy value that wasn't even the enum). These tests pin the
declared tool list, policies, risk levels, table list, and version stamps to
the code so the freeze can't recur.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from plugin_playbooks import PlaybooksPlugin
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.delegation import build_delegation_tools
from plugin_playbooks.models import Base

_ROOT = Path(__file__).resolve().parent.parent


class _Bus:
    async def emit(self, name, payload):
        pass

    def subscribe(self, name, handler, background=False):
        return lambda: None


class _StubRunner:
    _tools = None
    _agent = None


def _toml() -> dict:
    with open(_ROOT / "plugin_playbooks" / "luna-plugin.toml", "rb") as f:
        return tomllib.load(f)


def _code_tooldefs():
    tds = [td for td, _ in build_tools(None, _Bus(), _StubRunner())]
    # plans/020: the delegation tools ship in the manifest too. Building the
    # defs needs no ctx/session — those are only touched inside the handlers.
    tds += [td for td, _ in build_delegation_tools(
        None, None, PlaybooksPlugin.AUTHORING_TOOLS,
    )]
    return {td.name: td for td in tds}


def test_toml_tools_match_code():
    manifest = _toml()
    declared = {t["name"]: t for t in manifest["tools"]}
    real = _code_tooldefs()
    assert set(declared) == set(real)
    for name, td in real.items():
        # the conftest ToolDef stub holds only kwargs actually passed; the
        # real SDK defaults are policy="auto_approve", risk_level="low"
        assert declared[name]["policy"] == getattr(td, "policy", "auto_approve"), name
        assert declared[name]["risk_level"] == getattr(td, "risk_level", "low"), name
    assert manifest["requires"]["tools"] == len(real)


def test_toml_tables_match_models():
    manifest = _toml()
    assert set(manifest["db_tables"]) == set(Base.metadata.tables)
    assert manifest["requires"]["tables"] == len(Base.metadata.tables)


def test_version_stamps_agree():
    """The three stamps (in-code manifest, luna-plugin.toml, pyproject.toml)
    must carry the same version — a toml-only bump makes upgrades look like
    they never applied."""
    toml_v = _toml()["version"]
    with open(_ROOT / "pyproject.toml", "rb") as f:
        py_v = tomllib.load(f)["project"]["version"]
    src = (_ROOT / "plugin_playbooks" / "__init__.py").read_text()
    assert f'version="{toml_v}"' in src
    assert py_v == toml_v


def test_owner_facing_tools_lead_with_why():
    """plans/018 phase 3 (trimmed by 021): mutations whose card/audit line
    the owner reads carry an optional `why`, FIRST in the schema so the
    presentation leads with plain language."""
    real = _code_tooldefs()
    for name in ("playbook_manifest_set",
                 "playbook_spec_delete", "playbook_set_autonomy"):
        props = real[name].parameters["properties"]
        assert next(iter(props)) == "why", name
        assert "OWNER" in props["why"]["description"], name
        assert "why" not in real[name].parameters.get("required", []), name
