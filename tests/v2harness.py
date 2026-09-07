"""plans/032 phase 04 test harness: build_tools on a REAL PlaybookRunner
whose `code_run` is the scripted shim fake from test_v2_loop, plus the
approval/ctx stubs of the lifecycle repro (tests/test_repro_fixplaybooks_
lifecycle.py:30-103). Shared by the phase 04 exit tests."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugin_playbooks import _ensure_columns
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.models import Base
from plugin_playbooks.runner import PlaybookRunner
from test_repro_fixplaybooks_lifecycle import _Approvals, _Ctx
from test_v2_checker import EXAMPLE as PY_CODE
from test_v2_loop import ScriptedCodeRun, _effect, _error, _Tool, _Tools

__all__ = [
    "PY_CODE", "CODE", "Bus", "Env", "env", "ScriptedCodeRun", "_effect",
    "_error", "_Approvals", "_Ctx",
]

# the pblang greeter of the lifecycle repro, on a tool the harness registers
CODE = (
    "playbook(name='greeter', description='says hi')\n"
    "say = tool('echo', message=inputs.greeting)\n"
)


class Bus:
    """Records emits AND subscriptions (the trigger service subscribes)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.handlers: dict[str, list] = {}

    async def emit(self, name: str, payload: dict) -> None:
        self.events.append((name, payload))

    def subscribe(self, name: str, handler, background: bool = False):
        self.handlers.setdefault(name, []).append(handler)
        return lambda: self.handlers[name].remove(handler)

    def named(self, name: str) -> list[dict]:
        return [p for n, p in self.events if n == name]


class Env:
    def __init__(self, engine, sf, tools, defs, bus, runner, code_run, approvals, ctx, registry):
        self.engine = engine
        self.sf = sf
        self.tools = tools
        self.defs = defs
        self.bus = bus
        self.runner = runner
        self.code_run = code_run
        self.approvals = approvals
        self.ctx = ctx
        self.registry = registry

    async def dispose(self) -> None:
        await self.engine.dispose()


def _return_ok(env: dict) -> dict:
    return {"kind": "return", "value": "ok"}


async def env(*, script=None, decision: str = "approved", **fake_tools) -> Env:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_columns(engine)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    registry = _Tools(**{k: _Tool(v) for k, v in fake_tools.items()})
    code_run = ScriptedCodeRun(script or _return_ok)
    registry.add("code_run", code_run.handler)
    bus = Bus()
    runner = PlaybookRunner(session_factory=sf, tool_registry=registry, events=bus)
    approvals = _Approvals(decision=decision)
    ctx = _Ctx(approvals)
    pairs = build_tools(sf, bus, runner, ctx)
    tools = {td.name: h for td, h in pairs}
    defs = {td.name: td for td, _ in pairs}
    return Env(engine, sf, tools, defs, bus, runner, code_run, approvals, ctx, registry)
