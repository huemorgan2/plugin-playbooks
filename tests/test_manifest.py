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


# 0.30.0 (plans/018 phase 3): tool/table counts, names, and policies are no
# longer frozen here — tests/test_manifest_drift.py pins the whole manifest
# to the actual ToolDefs and Base.metadata, so the toml can't drift again
# (this file's frozen copies were HOW it drifted from 0.26 to 0.29).


def test_tool_and_table_counts_are_internally_consistent():
    assert MANIFEST["requires"]["tools"] == len(MANIFEST["tools"])
    assert MANIFEST["requires"]["tables"] == len(MANIFEST["db_tables"])


def test_tool_policies():
    tools = {t["name"]: t for t in MANIFEST["tools"]}
    assert tools["playbook_set_autonomy"]["policy"] == "prompt_always"
    assert tools["playbook_agent"]["risk_level"] == "medium"


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
