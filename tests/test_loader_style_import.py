"""The package must import the way luna's plugin loader imports it.

luna (`luna/plugins/loader.py::_import_module`) loads image-set and managed
plugins with `spec_from_file_location` under the synthetic module name
`luna_plugin_<dirname>`, so no module named `plugin_playbooks` exists in the
process. Any absolute self-import (`from plugin_playbooks.x import y`) therefore
raises ModuleNotFoundError at on_load and luna falls back to the previously
managed copy — exactly what voided the first dojop/01 bench run on 0.49.0.

This test reproduces that load path in a subprocess (fresh interpreter, real
`plugin_playbooks` name blocked by a meta-path finder, luna_sdk stub from
conftest) and imports every submodule under the synthetic name.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "plugin_playbooks"

_SCRIPT = r"""
import importlib, importlib.abc, importlib.util, pkgutil, sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])          # tests/ -> conftest installs the luna_sdk stub
import conftest  # noqa: F401

class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "plugin_playbooks" or name.startswith("plugin_playbooks."):
            raise ModuleNotFoundError(
                f"No module named {name!r} (blocked: luna loads plugins under a synthetic name)"
            )
        return None

sys.meta_path.insert(0, _Block())
for k in [k for k in sys.modules if k == "plugin_playbooks" or k.startswith("plugin_playbooks.")]:
    del sys.modules[k]

pkg_dir = Path(sys.argv[2])
mod_name = "luna_plugin_" + pkg_dir.name.replace("-", "_").replace(".", "_")
spec = importlib.util.spec_from_file_location(
    mod_name, str(pkg_dir / "__init__.py"), submodule_search_locations=[str(pkg_dir)]
)
mod = importlib.util.module_from_spec(spec)
sys.modules[mod_name] = mod
spec.loader.exec_module(mod)

failures = []
for info in pkgutil.walk_packages([str(pkg_dir)], mod_name + "."):
    try:
        importlib.import_module(info.name)
    except Exception as e:  # noqa: BLE001
        failures.append(f"{info.name}: {type(e).__name__}: {e}")

assert hasattr(mod, "PlaybooksPlugin"), "entry class missing under synthetic name"
print("IMPORTED", len(list(pkgutil.walk_packages([str(pkg_dir)], mod_name + "."))))
if failures:
    print("FAILURES")
    print("\n".join(failures))
    sys.exit(1)
"""


def test_every_module_imports_under_luna_synthetic_name(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(ROOT / "tests"), str(PKG)],
        cwd=tmp_path,  # not the repo root: `plugin_playbooks` must not be on sys.path
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "IMPORTED" in proc.stdout


def test_no_absolute_self_imports_in_source() -> None:
    """Static twin of the loader test: no `from plugin_playbooks...` / `import plugin_playbooks` lines."""
    offenders = []
    for py in PKG.rglob("*.py"):
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            s = line.strip()
            if s.startswith("from plugin_playbooks") or s.startswith("import plugin_playbooks"):
                offenders.append(f"{py.relative_to(ROOT)}:{lineno}: {s}")
    assert not offenders, offenders
