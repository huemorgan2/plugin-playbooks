"""Real-jail harness for the v2 runtime tests (plans/032 phase 02, Step 4).

The suite never reaches plugin-inline-code-run's jail on its own
(`tests/conftest.py` installs only the `luna_sdk` stub, every other test
fakes `code_run`). This module imports the MANAGED INSTALL of
plugin-inline-code-run (`~/.luna/managed_plugins/plugin_inline_code_run`,
override with `LUNA_INLINE_CODE_RUN_DIR`) and reproduces `tool._run` minus
its plugin-ctx needs: same flags, backend, rlimits and run-dir layout.

`real_code_run(tmp_root)` returns a handler with the `code_run` tool
signature; `jail_available()` gates the `real_jail` marker.
"""

from __future__ import annotations

import functools
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

_PKG = "plugin_inline_code_run"


def _install_dir() -> Path:
    override = os.environ.get("LUNA_INLINE_CODE_RUN_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".luna" / "managed_plugins" / _PKG


@functools.lru_cache(maxsize=1)
def _import_install():
    """Import the managed install as the package `plugin_inline_code_run`."""
    d = _install_dir()
    if not (d / "runner.py").is_file():
        return None
    parent = str(d.parent)
    if d.name != _PKG:
        # a throwaway copy under another name: alias it
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            _PKG, d / "__init__.py", submodule_search_locations=[str(d)],
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = mod
        spec.loader.exec_module(mod)
        return mod
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        import importlib

        return importlib.import_module(_PKG)
    except Exception:  # noqa: BLE001 — unavailable is a skip, not a failure
        return None


@functools.lru_cache(maxsize=1)
def jail_available() -> bool:
    mod = _import_install()
    if mod is None:
        return False
    try:
        from plugin_inline_code_run import probe

        return bool(probe.jail_status().available)
    except Exception:  # noqa: BLE001
        return False


real_jail = pytest.mark.real_jail


def requires_jail():
    return pytest.mark.skipif(
        not jail_available(),
        reason="needs the plugin-inline-code-run managed install and a usable kernel jail",
    )


def real_code_run(tmp_root: Path):
    """A registry handler with the `code_run` signature, jailed for real.

    Payload = `tool.py:175-195` (`ok`, `exit_code`, `stdout`, `stderr`,
    `timed_out`, `duration_ms`, `backend`, `result`/`result_error`,
    `output_files: []`) plus `progress` read straight from the run dir — a
    test-only door; production reaches progress.json only via a storage
    provider. `calls` on the handler records every invocation.
    """
    _import_install()
    from plugin_inline_code_run import json_mode, run_dir, runner
    from plugin_inline_code_run.settings import Settings

    scratch = Path(tmp_root) / "code_run"
    scratch.mkdir(parents=True, exist_ok=True)
    settings = Settings()

    async def handler(
        code: str, input_json: Any = None, timeout_sec: int | None = None,
        title: str | None = None, inputs: Any = None, **_ignored: Any,
    ) -> str:
        handler.calls.append({
            "code": code, "input_json": input_json, "timeout_sec": timeout_sec,
            "title": title,
        })
        run_id, rdir = run_dir.new_run_dir(scratch)
        exec_code = code
        if input_json is not None:
            json_mode.write_input(rdir, input_json)
            exec_code = json_mode.wrap(code)
        t0 = time.monotonic()
        result = await runner.run(
            exec_code, rdir,
            limits=settings.run_limits(timeout_sec),
            runtime_python=None,
            run_id=run_id,
        )
        ok = result.exit_code == 0 and not result.timed_out
        payload: dict[str, Any] = {
            "ok": ok,
            "title": title or "code",
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.output_truncated,
            "timed_out": result.timed_out,
            "duration_ms": result.duration_ms,
            "backend": result.backend,
            "run_id": run_id,
            "output_files": [],
            "output_dropped": 0,
            "input_errors": [],
            "host_ms": int((time.monotonic() - t0) * 1000),
        }
        if input_json is not None and ok:
            json_result, json_err = json_mode.read_result(rdir)
            if json_err is None:
                payload["result"] = json_result
            else:
                payload["result_error"] = json_err
        progress = rdir / "outputs" / "progress.json"
        if progress.is_file():
            try:
                payload["progress"] = json.loads(progress.read_text(encoding="utf-8"))
            except ValueError:
                pass
        handler.last_run_dir = rdir
        handler.payloads.append(payload)
        run_dir.finalize(rdir, failed=not ok, keep_hours=0)
        return json.dumps(payload, default=str)

    handler.calls = []
    handler.payloads = []
    handler.last_run_dir = None
    return handler
