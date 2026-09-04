"""plans/031 — tool_call steps resolve `vault:<name>` refs before the handler.

The agent dispatch gate resolves vault refs for direct tool calls, but the
playbook runner invokes handlers straight from the registry — so a header
like {"x-api-key": "vault:my_key"} went to the remote API as the literal
string and got 401. These tests pin the fix: the handler receives the secret,
persisted step inputs keep the ref, and failures are loud.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks.models import Base, Playbook, PlaybookStepRun
from plugin_playbooks.runner import PlaybookRunner


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))


class _Tool:
    def __init__(self, handler) -> None:
        self.handler = handler


class _Tools:
    def __init__(self, **tools) -> None:
        self._tools = tools

    def get(self, name: str) -> _Tool:
        return self._tools[name]


class _Cred:
    def __init__(self, value: str) -> None:
        self.value = value


class _Vault:
    def __init__(self, creds: dict[str, str], denied: set[str] = frozenset()) -> None:
        self._creds = creds
        self._denied = set(denied)
        self.reads: list[str] = []

    async def get_credential(self, name: str) -> _Cred:
        self.reads.append(name)
        if name in self._denied:
            raise PermissionError(f"credential '{name}' not granted.")
        return _Cred(self._creds[name])


class _Ctx:
    current_conversation_id = None

    def __init__(self, vault) -> None:
        self.vault = vault


def _playbook(name: str, steps: list[dict]) -> Playbook:
    return Playbook(
        name=name,
        display_name=name,
        definition={"name": name, "steps": steps},
        status="enabled",
    )


SECRET = "lsk_0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)

    seen: list[dict] = []

    async def http_request(**kw):
        seen.append(kw)
        return {"status": 200}

    vault = _Vault({"my_api_key": SECRET}, denied={"locked_key"})

    def make_runner(context):
        return PlaybookRunner(
            session_factory=sf,
            tool_registry=_Tools(http_request=_Tool(http_request)),
            events=_Bus(),
            context=context,
        )

    yield sf, make_runner, vault, seen
    await engine.dispose()


async def _save(sf, pb: Playbook) -> Playbook:
    async with sf() as s:
        s.add(pb)
        await s.commit()
        await s.refresh(pb)
    return pb


async def _step_row(sf, run_id, step_id) -> PlaybookStepRun:
    async with sf() as s:
        return (await s.execute(
            select(PlaybookStepRun).where(
                PlaybookStepRun.run_id == run_id,
                PlaybookStepRun.step_id == step_id,
            )
        )).scalar_one()


async def test_ref_resolved_for_handler_but_persisted_as_ref(env):
    sf, make_runner, vault, seen = env
    runner = make_runner(_Ctx(vault))
    pb = await _save(sf, _playbook("digest", [
        {"id": "fetch", "kind": "tool_call", "tool": "http_request",
         "args": {"url": "https://x.test/t",
                  "headers": {"x-api-key": "vault:my_api_key"},
                  "note": "docs mention vault:my_api_key in prose"}},
    ]))

    run = await runner.start_run(pb, inputs={})
    assert run.status == "done"
    # Handler got the secret; only the exact-match ref resolved.
    assert seen[0]["headers"]["x-api-key"] == SECRET
    assert seen[0]["note"] == "docs mention vault:my_api_key in prose"
    assert vault.reads == ["my_api_key"]
    # The DB row keeps the ref, never the secret.
    row = await _step_row(sf, run.id, "fetch")
    assert row.inputs["headers"]["x-api-key"] == "vault:my_api_key"
    assert SECRET not in str(row.inputs) and SECRET not in str(row.outputs)


async def test_missing_credential_fails_step_loudly(env):
    sf, make_runner, vault, seen = env
    runner = make_runner(_Ctx(vault))
    pb = await _save(sf, _playbook("bad-ref", [
        {"id": "fetch", "kind": "tool_call", "tool": "http_request",
         "args": {"headers": {"x-api-key": "vault:nope"}}},
    ]))
    run = await runner.start_run(pb, inputs={})
    assert run.status == "failed"
    row = await _step_row(sf, run.id, "fetch")
    assert "'nope' not found" in (row.error or "")
    assert seen == []  # the tool was never called


async def test_denied_credential_names_the_grant(env):
    sf, make_runner, vault, seen = env
    runner = make_runner(_Ctx(vault))
    pb = await _save(sf, _playbook("denied-ref", [
        {"id": "fetch", "kind": "tool_call", "tool": "http_request",
         "args": {"headers": {"x-api-key": "vault:locked_key"}}},
    ]))
    run = await runner.start_run(pb, inputs={})
    assert run.status == "failed"
    row = await _step_row(sf, run.id, "fetch")
    assert "denied" in (row.error or "")
    assert "plugin-playbooks" in (row.error or "")
    assert seen == []


async def test_ref_without_vault_fails_not_leaks(env):
    sf, make_runner, vault, seen = env
    runner = make_runner(context=None)
    pb = await _save(sf, _playbook("no-vault", [
        {"id": "fetch", "kind": "tool_call", "tool": "http_request",
         "args": {"headers": {"x-api-key": "vault:my_api_key"}}},
    ]))
    run = await runner.start_run(pb, inputs={})
    assert run.status == "failed"
    row = await _step_row(sf, run.id, "fetch")
    assert "no vault" in (row.error or "")
    assert seen == []  # the literal ref must never reach the tool


async def test_refless_args_skip_resolution(env):
    sf, make_runner, vault, seen = env
    runner = make_runner(context=None)  # no vault needed when no refs
    pb = await _save(sf, _playbook("plain", [
        {"id": "fetch", "kind": "tool_call", "tool": "http_request",
         "args": {"url": "https://x.test", "headers": {"a": "b"}}},
    ]))
    run = await runner.start_run(pb, inputs={})
    assert run.status == "done"
    assert vault.reads == []
