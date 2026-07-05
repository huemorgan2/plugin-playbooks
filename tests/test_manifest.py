"""Manifest contract tests for the standalone plugin-playbooks repo.

These run without Luna core installed — they only parse the TOML and the package
tree, asserting the published shape stays in sync.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "plugin_playbooks"
MANIFEST = tomllib.loads((PKG / "luna-plugin.toml").read_text())


def test_identity():
    assert MANIFEST["name"] == "plugin-playbooks"
    assert MANIFEST["entry"] == "plugin_playbooks"
    assert MANIFEST["sdk_version"] == "0"
    assert MANIFEST["license"] == "MIT"
    assert MANIFEST["category"] == "system"


def test_tool_and_table_counts():
    assert MANIFEST["requires"]["tools"] == 10
    assert len(MANIFEST["tools"]) == 10
    assert MANIFEST["requires"]["tables"] == 5
    assert len(MANIFEST["db_tables"]) == 5


def test_db_table_names():
    assert set(MANIFEST["db_tables"]) == {
        "playbooks",
        "playbook_versions",
        "playbook_runs",
        "playbook_step_runs",
        "playbook_drafts",
    }


def test_tool_policies():
    tools = {t["name"]: t for t in MANIFEST["tools"]}
    assert tools["playbook_run"]["risk_level"] == "medium"
    assert tools["playbook_set_autonomy"]["policy"] == "ask"
    low_auto = {n for n, t in tools.items() if t["policy"] == "auto_approve" and t["risk_level"] == "low"}
    assert len(low_auto) == 8


def test_no_core_imports():
    offenders = []
    for py in PKG.rglob("*.py"):
        for line in py.read_text().splitlines():
            s = line.strip()
            if s.startswith(("import luna", "from luna")) and "luna_sdk" not in s:
                offenders.append(f"{py.name}: {s}")
    assert not offenders, offenders


def test_ships_prebuilt_ui():
    assert (PKG / "ui" / "index.html").exists()
    assets = list((PKG / "ui" / "assets").glob("*.js"))
    assert assets, "hashed JS bundle missing from ui/assets"
