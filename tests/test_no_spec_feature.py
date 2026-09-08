"""0.47.0 — the specs (Tests tab) feature is gone and stays gone.

Owner decision 2026-09-07 (luna-fixer plans/2026-09-06-fix-playbooks §2
Specs removal). This file is the guard: any hook of the feature creeping
back into the package, the skill bodies, the delegate prompt, the docs, or
the built UI bundle fails here, and the DB upgrade that drops the remnants
is proven idempotent.

ALLOWLIST (the only places the bare word "spec" may still appear):
- `reference.py` — "the full spec" = the pblang language reference, and the
  Jinja `Tests: is defined, …` line in the cheatsheet.
- `agent_tools.py` — two comments about the language-reference "spec" handed
  to the agent on failed validation.
- `runner.py` / `testing.py` — the stub-for-type helpers' `spec` parameter
  (a JSON-schema "spec", not a playbook spec).
- `pblang/compiler.py` — "f-string format specs".
- `__init__.py` `_drop_spec_remnants` (+ its on_load call and warning) —
  necessarily names `playbook_specs` / `publish_require_specs` to DROP them.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import (
    _AUTHORING_SKILL_BODY,
    _DELEGATION_SKILL_BODY,
    PlaybooksPlugin,
    _drop_spec_remnants,
)
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.delegation import (
    _GATED_TOOL_OWNER_WORDS,
    _GATED_TOOLS,
    _PHASE_BY_TOOL,
    _delegate_prompt,
    build_delegation_tools,
)
from plugin_playbooks.models import Base, Playbook
from plugin_playbooks.runner import PlaybookRunner
from evidence import EXPLANATION

_ROOT = Path(__file__).resolve().parent.parent
_PKG = _ROOT / "plugin_playbooks"

# --- item 1: the feature's identifiers, word-bounded ------------------------

_FEATURE_TOKENS = [
    "playbook_spec", "PlaybookSpec", "playbook_specs", "specs_gate",
    "spec_from_run", "carried_from", "require_specs", "publish_require_specs",
    "run_all_specs",
    "copy_specs", "spec_source_version", "specsLabel", "specsHeadline",
    "SpecEntry", "getSpecs", "runSpecs", "Tests", "all specs",
]
_FEATURE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _FEATURE_TOKENS) + r")\b"
)
_BARE_SPEC_RE = re.compile(r"\bspecs?\b", re.IGNORECASE)

# (path relative to package, substring identifying the allowlisted line)
_JSON_SCHEMA_SPEC = (
    "(spec: Any)", "isinstance(spec, dict)", "spec.get(\"type\"", "str(spec)",
)
_BARE_SPEC_ALLOW = {
    "reference.py": ("full spec", "Tests: is defined"),
    "agent_tools.py": ("hand it the spec", "attach the spec"),
    "runner.py": _JSON_SCHEMA_SPEC,
    "testing.py": _JSON_SCHEMA_SPEC,
    "pblang/compiler.py": ("format specs",),
    # + the `_drop_spec_remnants` body, by line range (`_allowed_lines`)
    "__init__.py": ("spec remnant",),
}
# Line-substring allowlist for the feature tokens.
_FEATURE_ALLOW = {
    "reference.py": ("Tests: is defined",),  # Jinja tests, not the feature
}


def _function_lines(src: str, name: str) -> range:
    """Line range (1-based, inclusive) of top-level function `name` in `src`.

    `_drop_spec_remnants` is the one place the feature's table/column names
    may be spelled out in the package — to DROP them. Allowing that function's
    body only (not the whole module) means a re-added `_COLUMN_MIGRATIONS`
    entry or model column elsewhere in `__init__.py` still fails the guard.
    """
    import ast

    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return range(node.lineno, node.end_lineno + 1)
    raise AssertionError(f"{name} not found at module level")


def _py_sources() -> dict[str, str]:
    return {
        str(p.relative_to(_PKG)): p.read_text()
        for p in _PKG.rglob("*.py")
    }


def _feature_hits(
    text_: str, *, allow: tuple[str, ...] = (), allow_lines: range = range(0),
) -> list[str]:
    hits = []
    for i, line in enumerate(text_.splitlines(), 1):
        if i in allow_lines or any(a in line for a in allow):
            continue
        if _FEATURE_RE.search(line):
            hits.append(f"{i}: {line.strip()}")
    return hits


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, name, payload):
        self.events.append((name, payload))

    def subscribe(self, name, handler, background=False):
        return lambda: None


class _StubRunner:
    _tools = None
    _agent = None


def _tooldefs() -> dict:
    tds = [td for td, _ in build_tools(None, _Bus(), _StubRunner())]
    tds += [td for td, _ in build_delegation_tools(
        None, None, PlaybooksPlugin.AUTHORING_TOOLS,
    )]
    return {td.name: td for td in tds}


def _allowed_lines(rel: str, src: str) -> range:
    if rel == "__init__.py":
        return _function_lines(src, "_drop_spec_remnants")
    return range(0)


def test_package_has_no_feature_tokens():
    bad = {}
    for rel, src in _py_sources().items():
        hits = _feature_hits(
            src, allow=_FEATURE_ALLOW.get(rel, ()), allow_lines=_allowed_lines(rel, src),
        )
        if hits:
            bad[rel] = hits
    assert not bad, json.dumps(bad, indent=1)


def test_package_bare_spec_word_only_in_allowlist():
    bad = {}
    for rel, src in _py_sources().items():
        allow = _BARE_SPEC_ALLOW.get(rel, ())
        allow_lines = _allowed_lines(rel, src)
        hits = []
        for i, line in enumerate(src.splitlines(), 1):
            if i in allow_lines or any(a in line for a in allow):
                continue
            if _BARE_SPEC_RE.search(line):
                hits.append(f"{i}: {line.strip()}")
        if hits:
            bad[rel] = hits
    assert not bad, json.dumps(bad, indent=1)


def test_load_time_migrations_name_no_spec_remnant():
    # Guard item 2: the load-time DDL lists must never re-add the dropped
    # table or column (an entry here would race `_drop_spec_remnants`).
    from plugin_playbooks import _COLUMN_MIGRATIONS, _LEGACY_INDEXES

    assert not [m for m in _COLUMN_MIGRATIONS if "spec" in (m[0] + m[1])], _COLUMN_MIGRATIONS
    assert not [ix for ix in _LEGACY_INDEXES if "spec" in (ix[0] + ix[1])], _LEGACY_INDEXES
    assert not hasattr(Playbook, "publish_require_specs")
    assert "publish_require_specs" not in Playbook.__table__.columns


def test_skill_bodies_and_reference_have_no_feature_tokens():
    from plugin_playbooks import reference

    assert not _feature_hits(_AUTHORING_SKILL_BODY)
    assert not _feature_hits(_DELEGATION_SKILL_BODY)
    assert not _BARE_SPEC_RE.search(_AUTHORING_SKILL_BODY)
    assert not _BARE_SPEC_RE.search(_DELEGATION_SKILL_BODY)
    # The stubs section replaced the SPECS section in the authoring skill.
    assert "DRY-RUN STUBS" in _AUTHORING_SKILL_BODY
    for name in ("LANGUAGE_CHEATSHEET", "LANGUAGE_MINIREF"):
        body = getattr(reference, name)
        assert not _feature_hits(body, allow=("Tests:",)), name


def test_tooldefs_have_no_feature_tokens():
    real = _tooldefs()
    assert not any(n.startswith("playbook_spec") for n in real)
    for name, td in real.items():
        blob = td.description + json.dumps(td.parameters)
        assert not _feature_hits(blob), (name, _feature_hits(blob))
        assert not _BARE_SPEC_RE.search(blob), name


def test_delegate_prompt_has_no_feature_tokens():
    pb = Playbook(
        name="p", display_name="p", definition={"name": "p", "steps": []},
        status="enabled", manifest="INTENT: x",
    )
    for p in (_delegate_prompt("task", pb), _delegate_prompt("task", None)):
        assert not _feature_hits(p), _feature_hits(p)
        assert not _BARE_SPEC_RE.search(p)
        assert "stubs" in p  # the replacement: stubs from a real run


def test_card_docs_and_bundle_have_no_feature_tokens():
    files = [
        _PKG / "card.py",
        _ROOT / "README.md",
        _ROOT / "vision" / "vision.md",
        *sorted((_PKG / "ui" / "assets").glob("*.js")),
    ]
    assert any(f.suffix == ".js" for f in files), "built UI bundle missing"
    bad = {}
    for f in files:
        src = f.read_text()
        allow = ("specs feature was removed",) if f.name == "vision.md" else ()
        hits = _feature_hits(src, allow=allow)
        if f.suffix == ".js":
            for tok in ("require_specs", "/specs", "tests-header",
                        "version-specs", "No tests yet", "switch-require-specs"):
                if tok in src:
                    hits.append(tok)
        if hits:
            bad[str(f.relative_to(_ROOT))] = hits
    assert not bad, json.dumps(bad, indent=1)


# --- items 4-6: behaviour ---------------------------------------------------

CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('send_chat_message', message=inputs.greeting)\n"
)


class _Tool:
    def __init__(self, handler) -> None:
        self.handler = handler


class _Tools:
    def __init__(self, **tools) -> None:
        self._tools = tools

    def get(self, name):
        return self._tools[name]

    def names(self):
        return list(self._tools)


async def _noop(**_kw):
    return {"ok": True}


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    runner = PlaybookRunner(
        session_factory=sf,
        tool_registry=_Tools(send_chat_message=_Tool(_noop)),
        events=_Bus(),
    )
    handlers = {td.name: h for td, h in build_tools(sf, _Bus(), runner)}
    yield sf, handlers, engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_publish_gates_are_validation_test_run_probes(env):
    from datetime import datetime, timedelta, timezone

    from readstage import parse_read_stage
    from sqlalchemy import select

    from plugin_playbooks.models import PlaybookRun

    sf, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    read = parse_read_stage(await handlers["playbook_edit"](name="greeter"))
    out = json.loads(await handlers["playbook_edit"](
        name="greeter", ticket=read["ticket"],
        code=CODE.replace("inputs.greeting", "inputs.name"),
    ))
    assert "error" not in out, out
    later = datetime.now(timezone.utc) + timedelta(seconds=5)
    async with sf() as s:
        pb = (await s.execute(select(Playbook))).scalars().first()
        s.add(PlaybookRun(
            playbook_id=pb.id, playbook_version=2, status="done",
            trigger="agent-candidate", is_test=True, started_at=later,
            completed_at=later + timedelta(seconds=1),
        ))
        await s.commit()
    out = json.loads(await handlers["playbook_publish"](
        explanation=EXPLANATION, name="greeter",
    ))
    assert out["status"] == "published", out
    assert [g["gate"] for g in out["gates"]] == [
        "static_validation", "test_run", "probes",
    ]


def test_card_has_no_spec_wait_word():
    src = (_PKG / "card.py").read_text()
    m = re.search(r"WAIT_WORDS=\{(.*?)\}", src, re.S)
    assert m, "WAIT_WORDS block missing from card.py"
    keys = re.findall(r"(\w+):'", m.group(1))
    assert "playbook_spec_delete" not in keys
    assert set(keys) == set(_GATED_TOOL_OWNER_WORDS), keys


@pytest.mark.asyncio
async def test_set_autonomy_rejects_require_specs(env):
    _, handlers, _ = env
    td = _tooldefs()["playbook_set_autonomy"]
    assert "require_specs" not in td.parameters["properties"]
    assert "require_run" in td.parameters["properties"]
    await handlers["playbook_propose"](name="greeter", code=CODE)
    with pytest.raises(TypeError):
        await handlers["playbook_set_autonomy"](name="greeter", require_specs=True)


@pytest.mark.asyncio
async def test_dry_run_tool_scripts_stubs_by_step_id_and_tool_name(env):
    _, handlers, _ = env
    await handlers["playbook_propose"](name="greeter", code=CODE)
    td = _tooldefs()["playbook_dry_run"]
    assert "stubs" in td.parameters["properties"]
    assert "stubs" not in td.parameters.get("required", [])

    # JSON string, keyed by step id
    out = json.loads(await handlers["playbook_dry_run"](
        name="greeter", inputs='{"greeting": "hi"}',
        stubs=json.dumps({"say": {"echo": "by-step"}}),
    ))
    # plans/032 phase 09: a dry run reports `simulated` at the tool boundary
    assert out["status"] == "simulated", out
    assert out["references"]["say"]["stubbed"] is True
    assert out["references"]["say"]["result"] == {"echo": "by-step"}

    # dict, keyed by tool name
    out = json.loads(await handlers["playbook_dry_run"](
        name="greeter", inputs={"greeting": "hi"},
        stubs={"send_chat_message": {"echo": "by-tool"}},
    ))
    assert out["status"] == "simulated", out
    assert out["references"]["say"]["result"] == {"echo": "by-tool"}

    # step id wins over tool name
    out = json.loads(await handlers["playbook_dry_run"](
        name="greeter", inputs={"greeting": "hi"},
        stubs={"say": {"who": "step"}, "send_chat_message": {"who": "tool"}},
    ))
    assert out["references"]["say"]["result"] == {"who": "step"}

    # no stubs → the self-describing placeholder, unchanged
    out = json.loads(await handlers["playbook_dry_run"](
        name="greeter", inputs={"greeting": "hi"},
    ))
    assert out["references"]["say"]["_dry"] is True
    assert out["references"]["say"].get("stubbed") is not True

    # a non-object is refused, not silently ignored
    out = json.loads(await handlers["playbook_dry_run"](
        name="greeter", stubs="[1, 2]",
    ))
    assert "stubs must be a JSON object" in out["error"]
    out = json.loads(await handlers["playbook_dry_run"](
        name="greeter", stubs="{not json",
    ))
    assert out["error"] == "Invalid JSON stubs"


def test_delegation_maps_reference_only_registered_tools():
    real = _tooldefs()
    # `playbook_list_available_triggers` is registered on load via
    # `PlaybooksPlugin._register_tool` (__init__.py, on_load), not by
    # `build_tools`/`build_delegation_tools`, so `_tooldefs()` cannot see it
    # (and the manifest omits it — pre-existing, outside this removal's scope).
    stale = {"playbook_list_available_triggers"}
    assert set(_PHASE_BY_TOOL) - stale <= set(real), set(_PHASE_BY_TOOL) - set(real)
    assert set(_GATED_TOOLS) <= set(real)
    assert set(_GATED_TOOL_OWNER_WORDS) == set(_GATED_TOOLS)
    prompt_always = {
        n for n, td in real.items()
        if getattr(td, "policy", "auto_approve") == "prompt_always"
    }
    assert prompt_always == {"playbook_set_autonomy", "playbook_run_candidate"}
    assert prompt_always <= _GATED_TOOLS
    skills = {s.name: s for s in PlaybooksPlugin.manifest.skills}
    authoring = set(skills["playbook-authoring"].tools)
    assert authoring <= set(PlaybooksPlugin.AUTHORING_TOOLS)
    assert authoring - stale <= set(real)
    assert not any(t.startswith("playbook_spec") for t in PlaybooksPlugin.AUTHORING_TOOLS)


# --- the DB upgrade: drop the remnants, once --------------------------------

_LEGACY_SPECS_DDL = (
    "CREATE TABLE playbook_specs ("
    " id INTEGER PRIMARY KEY, playbook_id VARCHAR(36), name VARCHAR(200),"
    " spec JSON, playbook_version INTEGER)"
)


async def _table_names(engine) -> set[str]:
    from sqlalchemy import inspect

    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


async def _playbook_columns(engine) -> set[str]:
    from sqlalchemy import inspect

    async with engine.connect() as conn:
        return {
            c["name"] for c in await conn.run_sync(
                lambda c: inspect(c).get_columns("playbooks")
            )
        }


@pytest.mark.asyncio
async def test_drop_spec_remnants_removes_table_and_column(env, caplog):
    _, _, engine = env
    async with engine.begin() as conn:
        await conn.execute(text(_LEGACY_SPECS_DDL))
        await conn.execute(text(
            "INSERT INTO playbook_specs (playbook_id, name, spec, playbook_version)"
            " VALUES ('a', 's1', '{}', 1), ('a', 's2', '{}', 1)"
        ))
        await conn.execute(text(
            "ALTER TABLE playbooks ADD COLUMN publish_require_specs"
            " BOOLEAN NOT NULL DEFAULT TRUE"
        ))
    assert "playbook_specs" in await _table_names(engine)
    assert "publish_require_specs" in await _playbook_columns(engine)

    with caplog.at_level(logging.INFO, logger="plugin_playbooks"):
        await _drop_spec_remnants(engine)

    assert "playbook_specs" not in await _table_names(engine)
    assert "publish_require_specs" not in await _playbook_columns(engine)
    assert "playbook_specs (2 rows)" in caplog.text
    assert "0.47.0" in caplog.text


@pytest.mark.asyncio
async def test_drop_spec_remnants_is_idempotent(env, caplog):
    _, _, engine = env
    async with engine.begin() as conn:
        await conn.execute(text(_LEGACY_SPECS_DDL))
    await _drop_spec_remnants(engine)
    before = await _table_names(engine), await _playbook_columns(engine)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="plugin_playbooks"):
        await _drop_spec_remnants(engine)  # second load: nothing to do
    assert (await _table_names(engine), await _playbook_columns(engine)) == before
    assert "dropping playbook_specs" not in caplog.text


@pytest.mark.asyncio
async def test_fresh_db_never_creates_spec_remnants(env, caplog):
    _, _, engine = env
    assert "playbook_specs" not in Base.metadata.tables
    assert "playbook_specs" not in await _table_names(engine)
    assert "publish_require_specs" not in await _playbook_columns(engine)
    with caplog.at_level(logging.INFO, logger="plugin_playbooks"):
        await _drop_spec_remnants(engine)
    assert "dropping playbook_specs" not in caplog.text
