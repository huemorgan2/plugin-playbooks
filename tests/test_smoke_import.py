"""Every module in the package imports with only the luna_sdk stub present."""

from __future__ import annotations

import importlib
import pkgutil

import plugin_playbooks


def test_all_modules_import():
    failures = []
    for m in pkgutil.iter_modules(plugin_playbooks.__path__, "plugin_playbooks."):
        try:
            importlib.import_module(m.name)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{m.name}: {e}")
    assert not failures, failures


def test_plugin_entry_exports():
    assert hasattr(plugin_playbooks, "PlaybooksPlugin"), "package must expose the LunaPlugin entry"
    assert plugin_playbooks.PlaybooksPlugin.manifest.name == "plugin-playbooks"
