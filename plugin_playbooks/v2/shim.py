"""In-jail shim for v2 playbooks (plans/032 phase 02; docs/v2.md §6, §7, §11).

`SHIM_SOURCE` is the constant `code` argument of every `code_run` segment.
plugin-inline-code-run wraps it as the body of `__pb_main__(inputs)`
(managed install `json_mode.wrap`: 5-line prologue, body indented 4, epilogue
dumps the return value to `outputs/result.json`), where `inputs` is the
envelope the host wrote:

    {playbook, version, source, hash_seed, max_effects, call_sites, journal}

The shim re-execs itself under `PYTHONHASHSEED=<hash_seed>` (hash-seed pin,
option A), silences stdout, compiles the saved source under the stable
filename `playbook:<name>@v<N>`, replays the journal and runs `run()` until
the first effect with no journal row, then returns ONE result:

    {kind: "effect" | "gather" | "return" | "error", ..., handled: [seq, ...]}

`handled` (phase 03) lists the replayed failures the code caught and
proceeded past; the host re-stamps those rows `failed_handled`.

The playbook coroutine is driven directly (`coro.send`), not through an
event loop: `asyncio` is outside the checker's import whitelist, every
`await` inside `run()` is a ctx effect, and a plain generator step lets the
segment stop at an effect WITHOUT unwinding the author's `try`/`finally`
blocks (an exception-based exit would run `finally:` bodies — and any effect
inside them — in the exiting segment).

The source below is a module-level string so this file stays importable
without executing it; `tests/test_v2_loop.py` runs it through the real jail.
"""

from __future__ import annotations

from .dry import DRY_CLASSES_SOURCE

# phase 05: the in-jail `DryStub`/`DryStubError` classes are shared text
# with `v2/dry.py` (the host executes the same source); spliced in below.
_DRY_MARK = "# @@DRY_CLASSES@@"

_SHIM_TEMPLATE = r'''
import sys as _pb_sys, os as _pb_os
# --- hash-seed pin, option A (inline-code-run-plan §3): the jail starts
# python with -I, which ignores PYTHONHASHSEED; re-exec once without it.
if _pb_sys.flags.isolated:
    _pb_exe = _pb_sys.executable
    if not _pb_exe:
        return {"kind": "error", "error_type": "HashSeedPinFailed",
                "message": "sys.executable is empty inside the jail: cannot re-exec "
                           "under PYTHONHASHSEED (hash-seed option A). Option B is "
                           "needed: plugin-inline-code-run must pass PYTHONHASHSEED "
                           "itself (inline-code-run-plan §3).",
                "traceback": [], "playbook_line": 0,
                "last_completed_effect": None, "locals_preview": {}}
    try:
        _pb_os.execve(
            _pb_exe,
            [_pb_exe, "-s", "-B", "-u", "-P", _pb_os.path.abspath(_pb_sys.argv[0])],
            dict(_pb_os.environ, PYTHONHASHSEED=str(inputs["hash_seed"])),
        )
    except OSError as _pb_e:
        return {"kind": "error", "error_type": "HashSeedPinFailed",
                "message": f"execve failed inside the jail ({_pb_e!r}): cannot re-exec "
                           "under PYTHONHASHSEED (hash-seed option A). Option B is "
                           "needed: plugin-inline-code-run must pass PYTHONHASHSEED "
                           "itself (inline-code-run-plan §3).",
                "traceback": [], "playbook_line": 0,
                "last_completed_effect": None, "locals_preview": {}}
    # execve never returns on success; reaching here is a failure too
    return {"kind": "error", "error_type": "HashSeedPinFailed",
            "message": "execve returned; the playbook was not started unseeded.",
            "traceback": [], "playbook_line": 0,
            "last_completed_effect": None, "locals_preview": {}}

import io as _pb_io, json as _pb_json, re as _pb_re, traceback as _pb_tb
from datetime import datetime as _pb_datetime

# stdout is never an author channel (docs/v2.md §7): silence it before any
# playbook code runs; stderr stays for shim-internal failures.
try:
    _pb_sys.stdout = open(_pb_os.devnull, "w")
except OSError:
    _pb_sys.stdout = _pb_io.StringIO()

_pb_name = str(inputs["playbook"])
_pb_version = int(inputs["version"])
_pb_filename = f"playbook:{_pb_name}@v{_pb_version}"
_pb_journal = list(inputs["journal"])
_pb_max_effects = int(inputs.get("max_effects") or 200)
_pb_call_sites = list(inputs.get("call_sites") or [])
_pb_inputs = dict((_pb_journal[0] if _pb_journal else {}).get("inputs") or {})
_pb_source = str(inputs["source"])
_pb_VAULT_RE = _pb_re.compile(r"vault:[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}")
_pb_OPTIONS = ("_id", "_timeout", "_retry")
_pb_EFFECT_KINDS = ("tool", "llm", "agent", "subtask", "gather", "approve", "now",
                    "random", "log", "wait_event")


def _pb_progress(seq, phase):
    try:
        with open("outputs/progress.json", "w", encoding="utf-8") as f:
            _pb_json.dump({"seq": seq, "phase": phase}, f)
    except OSError:
        pass


_pb_progress(0, "replaying")


# --- exception family (docs/v2.md §4) ---
class EffectError(Exception):
    """Base class of every catchable effect failure."""


class ToolError(EffectError): ...
class EffectTimeout(EffectError): ...
class OutcomeUnknown(EffectError): ...
class Rejected(EffectError): ...
class ApprovalExpired(EffectError): ...
class EventTimeout(EffectError): ...
class SubtaskFailed(EffectError): ...


class RunCancelled(BaseException): ...
class JournalDivergence(BaseException): ...
class MaxEffectsExceeded(BaseException): ...


_pb_FAILED_CLASSES = {
    "ToolError": ToolError, "EffectTimeout": EffectTimeout,
    "OutcomeUnknown": OutcomeUnknown, "Rejected": Rejected,
    "ApprovalExpired": ApprovalExpired, "EventTimeout": EventTimeout,
    "SubtaskFailed": SubtaskFailed, "EffectError": EffectError,
}


class _pb_ArgsNotSerializable(Exception): ...


# @@DRY_CLASSES@@


class _pb_Request:
    """What an effect awaitable yields to the driver: one pending effect."""

    __slots__ = ("kind", "site", "name", "args", "options")

    def __init__(self, kind, site, name, args, options):
        self.kind, self.site, self.name, self.args, self.options = kind, site, name, args, options


class _pb_Awaitable:
    __slots__ = ("request",)

    def __init__(self, request):
        self.request = request

    def __await__(self):
        result = yield self.request
        return result


class _pb_GatherRequest:
    """What `ctx.gather(...)` yields: the pending requests in argument order."""

    __slots__ = ("requests",)

    def __init__(self, requests):
        self.requests = requests


class _pb_GatherAwaitable:
    __slots__ = ("requests",)

    def __init__(self, requests):
        self.requests = requests

    def __await__(self):
        if not self.requests:
            return []
        results = yield _pb_GatherRequest(self.requests)
        return results


class _pb_NotWired:
    __slots__ = ("message",)

    def __init__(self, message):
        self.message = message

    def __await__(self):
        raise EffectError(self.message)
        yield  # noqa — makes this a generator


_pb_sites_by_pos = {}
_pb_sites_by_line = {}
for _pb_s in _pb_call_sites:
    _pb_sites_by_pos[(int(_pb_s["line"]), int(_pb_s["col"]))] = _pb_s
    _pb_sites_by_line.setdefault(int(_pb_s["line"]), []).append(_pb_s)


def _pb_caller_site(frame):
    """Resolve the checker call site of the `ctx.*` call in `frame` by its
    (line, col) — the CALL instruction's start position equals the AST
    Call node's (lineno, col_offset) on 3.11+ (`co_positions`)."""
    if frame.f_code.co_filename != _pb_filename:
        raise JournalDivergence(
            f"ctx effect called from {frame.f_code.co_filename!r}, not from the "
            f"playbook {_pb_filename!r}: code edited under a run, or an effect "
            "reached through a non-playbook helper"
        )
    line = frame.f_lineno
    col = None
    try:
        positions = list(frame.f_code.co_positions())
        pos = positions[frame.f_lasti // 2]
        if pos and pos[0] is not None:
            line, col = pos[0], pos[2]
    except (AttributeError, IndexError, TypeError):
        col = None
    site = _pb_sites_by_pos.get((line, col)) if col is not None else None
    if site is None:
        same_line = _pb_sites_by_line.get(line) or []
        if len(same_line) == 1:
            site = same_line[0]
    if site is None:
        raise JournalDivergence(
            f"no call site at line {line}, col {col} of {_pb_filename}: the source "
            "was edited under a run (code edited under a run), or the call sites "
            "were derived from a different version — candidate causes: set "
            "iteration, code edited under a run, non-journaled randomness"
        )
    return site


def _pb_json_norm(value):
    # phase 05: a DryStub handed to the next effect journals as its
    # `<dry:...>` string (stable across segments, so replay compares equal)
    return _pb_json.loads(_pb_json.dumps(value, default=_pb_dry_json_default))


class _pb_Ctx:
    """The `ctx` object handed to `run()` (docs/v2.md §2)."""

    # the exception classes are attached after the class body (a class body
    # cannot read enclosing-function names it also assigns)

    def __repr__(self):
        return "<ctx>"

    # every effect method is SYNC: it captures the caller frame at call time
    # (so `ctx.gather(ctx.tool(...), ...)` and `await ctx.tool(...)` resolve
    # the same site) and returns an awaitable.
    def _effect(self, kind, frame, name, args):
        options = {k: args.pop(k) for k in _pb_OPTIONS if k in args}
        try:
            args = _pb_json_norm(args)
        except (TypeError, ValueError) as e:
            raise _pb_ArgsNotSerializable(
                f"ctx.{kind} arguments must be JSON-serializable: {e}"
            ) from None
        site = _pb_caller_site(frame)
        return _pb_Awaitable(_pb_Request(kind, site, name, args, options))

    def tool(self, name, /, **args):
        if not isinstance(name, str):
            raise TypeError("ctx.tool: the tool name must be a string literal")
        return self._effect("tool", _pb_sys._getframe(1), name, args)

    def now(self, **args):
        return self._effect("now", _pb_sys._getframe(1), None, args)

    def random(self, **args):
        return self._effect("random", _pb_sys._getframe(1), None, args)

    def log(self, msg, **args):
        args["message"] = msg if isinstance(msg, str) else str(msg)
        return self._effect("log", _pb_sys._getframe(1), None, args)

    # phase 03: the four awaited kinds. Every argument is journaled (JSON),
    # `output=` is v1's schema dict; the host executes with v1 parity.
    def llm(self, prompt, *, output=None, purpose=None, model=None, system=None, **options):
        if not isinstance(prompt, str):
            raise TypeError("ctx.llm: the prompt must be a string")
        args = {"prompt": prompt, "output": output, "purpose": purpose,
                "model": model, "system": system}
        args.update(options)
        return self._effect("llm", _pb_sys._getframe(1), None, args)

    def agent(self, prompt, *, output=None, tools=None, **options):
        if not isinstance(prompt, str):
            raise TypeError("ctx.agent: the prompt must be a string")
        args = {"prompt": prompt, "output": output, "tools": tools}
        args.update(options)
        return self._effect("agent", _pb_sys._getframe(1), None, args)

    def subtask(self, playbook, inputs=None, *, returns=None, **options):
        if not isinstance(playbook, str):
            raise TypeError("ctx.subtask: the playbook name must be a string literal")
        args = {"inputs": dict(inputs or {}), "returns": returns}
        args.update(options)
        return self._effect("subtask", _pb_sys._getframe(1), playbook, args)

    def approve(self, *, show, **options):
        args = {"show": show}
        args.update(options)
        return self._effect("approve", _pb_sys._getframe(1), None, args)

    def gather(self, *handles):
        requests = []
        for h in handles:
            if not isinstance(h, _pb_Awaitable):
                raise TypeError(
                    "ctx.gather takes un-awaited ctx effect calls only, got "
                    f"{type(h).__name__}"
                )
            requests.append(h.request)
        return _pb_GatherAwaitable(requests)

    def wait_event(self, name, filter=None, *, timeout, **options):
        # phase 07: the park form — the host parks the run on a bus
        # subscription; replay: done → payload, failed/EventTimeout → raise.
        if not isinstance(name, str):
            raise TypeError("ctx.wait_event: the event name must be a string literal")
        if filter is not None and not isinstance(filter, dict):
            raise TypeError("ctx.wait_event: filter must be a dict of payload keys")
        args = {"name": name, "filter": dict(filter) if filter else None, "timeout": timeout}
        args.update(options)
        return self._effect("wait_event", _pb_sys._getframe(1), name, args)


for _pb_cls in (EffectError, ToolError, EffectTimeout, OutcomeUnknown, Rejected,
                ApprovalExpired, EventTimeout, SubtaskFailed, RunCancelled,
                JournalDivergence, MaxEffectsExceeded):
    setattr(_pb_Ctx, _pb_cls.__name__, _pb_cls)
_pb_ctx = _pb_Ctx()
_pb_occurrences = {}
_pb_last_effect = None  # {seq, id, kind} of the last journal entry consumed
# phase 03 `failed_handled`: seqs of replayed failures the code proceeded past
# (the next effect was awaited or `run()` returned) — reported on every exit.
_pb_handled = []


def _pb_decode_result(kind, entry):
    result = entry.get("result")
    if entry.get("dry") and kind in ("tool", "llm", "agent", "subtask", "wait_event"):
        # phase 05: rebuild the placeholder (or wrap the provided stub) from
        # the dry journal row: {dry, stubbed, stub_key, schema, effect}
        effect = entry.get("effect") or f"{entry.get('id')}#{entry.get('occurrence')}"
        if entry.get("stubbed"):
            return _pb_dry_wrap(result, effect, entry.get("stub_key") or effect)
        return DryStub(effect, "", entry.get("schema"))
    if kind == "now" and isinstance(result, str):
        return _pb_datetime.fromisoformat(result)
    if kind == "log":
        return None
    return result


_pb_source_lines = _pb_source.splitlines()


def _pb_source_line(n):
    if n and 1 <= n <= len(_pb_source_lines):
        return _pb_source_lines[n - 1].strip()
    return ""


def _pb_mask(text):
    return _pb_VAULT_RE.sub("vault:***", text)


def _pb_error_payload(exc):
    tb = exc.__traceback__
    frames = []
    innermost = None
    for fs, cur in zip(_pb_tb.extract_tb(tb), _pb_iter_tb(tb)):
        if fs.filename == _pb_filename:
            frames.append({"line": fs.lineno, "name": fs.name,
                           "source": _pb_source_line(fs.lineno)})
            innermost = cur
    playbook_line = frames[-1]["line"] if frames else 0
    if isinstance(exc, SyntaxError) and exc.filename == _pb_filename and exc.lineno:
        playbook_line = exc.lineno
        frames.append({"line": exc.lineno, "name": "<module>",
                       "source": (exc.text or "").strip()})
    preview = {}
    if innermost is not None:
        for k, v in list(innermost.tb_frame.f_locals.items()):
            if k == "ctx" or v is _pb_ctx:
                preview[k] = "<ctx>"
                continue
            try:
                r = repr(v)
            except Exception:  # noqa: BLE001
                r = f"<unrepr {type(v).__name__}>"
            preview[k] = _pb_mask(r[:200])
    message = _pb_mask(str(exc))
    return {
        "kind": "error",
        "error_type": type(exc).__name__,
        "message": message,
        "traceback": frames,
        "playbook_line": playbook_line,
        "last_completed_effect": _pb_last_effect,
        "locals_preview": preview,
        "handled": list(_pb_handled),
    }


def _pb_iter_tb(tb):
    while tb is not None:
        yield tb
        tb = tb.tb_next


def _pb_serve(request, cursor):
    """Replay `request` against journal[cursor] or exit the segment."""
    nonlocal _pb_last_effect
    site_id = request.site["id"]
    occ = _pb_occurrences.get(site_id, 0) + 1
    _pb_occurrences[site_id] = occ
    key = f"{site_id}#{occ}"
    seq = cursor
    if seq < len(_pb_journal):
        entry = _pb_journal[seq]
        expected = (entry.get("kind"), entry.get("id"), entry.get("occurrence"),
                    _pb_json_norm(entry.get("args")))
        actual = (request.kind, site_id, occ, request.args)
        if expected != actual:
            raise JournalDivergence(
                f"journal entry {seq} is {entry.get('kind')} "
                f"{entry.get('id')}#{entry.get('occurrence')} args={entry.get('args')!r} "
                f"but the code asked for {request.kind} {key} args={request.args!r} — "
                "candidate causes: set iteration, code edited under a run, "
                "non-journaled randomness"
            )
        _pb_last_effect = {"seq": seq, "id": key, "kind": request.kind}
        status = entry.get("status")
        if status == "done":
            return ("value", _pb_decode_result(request.kind, entry))
        if status in ("failed", "failed_handled"):
            err = entry.get("error") or {}
            cls = _pb_FAILED_CLASSES.get(err.get("type"), EffectError)
            return ("raise", cls(err.get("message") or err.get("type") or "effect failed"))
        if status in ("in_flight", "timed_out_unknown", "parked"):
            # `parked` (phase 07) is unreachable in practice — the host
            # completes/fails the parking entry before the next segment.
            return ("raise", OutcomeUnknown(
                f"the outcome of {key} is unknown (journal status {status})"))
        return ("raise", EffectError(f"journal entry {seq} has status {status!r}"))
    if seq > _pb_max_effects:
        raise MaxEffectsExceeded(
            f"effect {key} would be effect #{seq}, past the cap of "
            f"{_pb_max_effects} effects per run (MAX_EFFECTS)"
        )
    _pb_progress(seq, "effect_exit")
    return ("exit", {
        "kind": "effect", "seq": seq, "id": key, "call_site_id": site_id,
        "occurrence": occ, "effect_kind": request.kind, "name": request.name,
        "args": request.args, "options": request.options,
    })


def _pb_main():
    nonlocal _pb_last_effect
    ns = {"__name__": "__playbook__", "__builtins__": __builtins__}
    try:
        code = compile(_pb_source, _pb_filename, "exec")
        exec(code, ns)
    except BaseException as e:  # noqa: BLE001 — every case is a payload
        return _pb_error_payload(e)
    run = ns.get("run")
    if run is None or not callable(run):
        return {"kind": "error", "error_type": "InvalidPlaybook",
                "message": "the playbook defines no `async def run(ctx, inputs)`",
                "traceback": [], "playbook_line": 0,
                "last_completed_effect": None, "locals_preview": {}}
    try:
        coro = run(_pb_ctx, _pb_inputs)
        if not hasattr(coro, "send"):
            return {"kind": "error", "error_type": "InvalidPlaybook",
                    "message": "`run` must be `async def run(ctx, inputs)`",
                    "traceback": [], "playbook_line": 0,
                    "last_completed_effect": None, "locals_preview": {}}
        cursor = 1
        send_value = None
        throw_exc = None
        thrown_seqs = []  # failures thrown into run() that are not yet known handled
        replay_done = len(_pb_journal) <= 1
        if replay_done:
            _pb_progress(0, "compute")
        while True:
            try:
                if throw_exc is not None:
                    exc, throw_exc = throw_exc, None
                    request = coro.throw(exc)
                else:
                    request = coro.send(send_value)
            except StopIteration as stop:
                # run() returned past every thrown failure: all handled
                _pb_handled.extend(thrown_seqs)
                thrown_seqs = []
                value = stop.value
                try:
                    value = _pb_json_norm(value)
                except (TypeError, ValueError) as e:
                    return {"kind": "error", "error_type": "ResultNotSerializable",
                            "message": f"run() returned a value that is not JSON: {e}",
                            "traceback": [], "playbook_line": 0,
                            "last_completed_effect": _pb_last_effect,
                            "locals_preview": {}, "handled": list(_pb_handled)}
                return {"kind": "return", "value": value, "handled": list(_pb_handled)}
            # the code proceeded to its next effect: every thrown failure was caught
            _pb_handled.extend(thrown_seqs)
            thrown_seqs = []
            if isinstance(request, _pb_GatherRequest):
                served = []
                for req in request.requests:
                    served.append((cursor, _pb_serve(req, cursor)))
                    cursor += 1
                if cursor >= len(_pb_journal) and not replay_done:
                    replay_done = True
                    _pb_progress(cursor - 1, "compute")
                exits = [payload for _, (action, payload) in served if action == "exit"]
                if exits:
                    return {"kind": "gather", "seq": exits[0]["seq"], "effects": exits,
                            "handled": list(_pb_handled)}
                raises = [(seq, payload) for seq, (action, payload) in served if action == "raise"]
                if raises:
                    # every element settled; the lowest-seq failure is raised
                    thrown_seqs = [seq for seq, _ in raises]
                    throw_exc = raises[0][1]
                else:
                    send_value = [payload for _, (_, payload) in served]
                continue
            if not isinstance(request, _pb_Request):
                raise JournalDivergence(
                    f"run() awaited something that is not a ctx effect: {request!r}"
                )
            action, payload = _pb_serve(request, cursor)
            served_seq = cursor
            cursor += 1
            if cursor >= len(_pb_journal) and not replay_done:
                # every journaled row is consumed: pure compute from here
                replay_done = True
                _pb_progress(cursor - 1, "compute")
            if action == "value":
                send_value = payload
            elif action == "raise":
                throw_exc = payload
                thrown_seqs = [served_seq]
            else:
                payload["handled"] = list(_pb_handled)
                return payload
    except BaseException as e:  # noqa: BLE001 — every case is a payload
        return _pb_error_payload(e)


return _pb_main()
'''

SHIM_SOURCE = _SHIM_TEMPLATE.replace(_DRY_MARK, DRY_CLASSES_SOURCE.strip("\n"), 1)
assert _DRY_MARK not in SHIM_SOURCE
