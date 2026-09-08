"""Dry run for v2 playbooks (plans/032 phase 05; docs/v2.md §10).

A dry run is the same segment loop with `mode="dry"`: the host answers every
effect from `stubs` — keyed per occurrence `<call-site id>#<n>` — or with a
`DryStub` placeholder; nothing executes; no run rows are written.

Two halves live here:

- `DRY_CLASSES_SOURCE` — the in-jail `DryStub` / `DryStubError` classes and
  their helpers, stdlib only. `SHIM_SOURCE` embeds this text (the shim is
  the one program sent to `code_run`), and this module also executes it so
  the host (and the unit tests) see the same classes.
- the host side: `resolve_stub`, `dry_answer`, `DRY_NOW`, `DRY_BANNER`.
"""

from __future__ import annotations

import random
from typing import Any

DRY_NOW = "2000-01-01T00:00:00+00:00"
DRY_BANNER = (
    "DRY RUN — tool/LLM outputs are SIMULATED. Do NOT report any "
    "value below as a real result."
)
STUBBED_KINDS = ("tool", "llm", "agent", "subtask")

# Names the jail and the host share verbatim. Everything is prefixed
# `_pb_dry_` except the two public class names, which `ctx` never exposes —
# they reach playbook code only through values and exceptions.
DRY_CLASSES_SOURCE = r'''
class DryStubError(Exception):
    """A dry run read data no stub provides (docs/v2.md §10)."""


def _pb_dry_key_path(path, key):
    if isinstance(key, int) and not isinstance(key, bool):
        return f"{path}[{key}]"
    return f"{path}.{key}"


def _pb_dry_show(path):
    return path.lstrip(".") or "value"


def _pb_dry_sample(effect, path, schema):
    """A typed sample for a declared output field: a real str/int/float/bool
    so the value composes (`"".join`, arithmetic); containers stay navigable."""
    if isinstance(schema, dict):
        t = schema.get("type")
        if t is None:
            return DryStub(effect, path, schema)
        if isinstance(t, str):
            t = t.lower()
        if t == "object":
            props = schema.get("properties")
            return DryStub(effect, path, props if isinstance(props, dict) else None)
        if t == "array":
            return [_pb_dry_sample(effect, f"{path}[0]", schema.get("items"))]
        return _pb_dry_sample(effect, path, t)
    if isinstance(schema, str):
        t = schema.lower()
        if t in ("str", "string", "text"):
            return f"<dry:{effect}{path}>"
        if t in ("int", "integer"):
            return 0
        if t in ("float", "number"):
            return 0.0
        if t in ("bool", "boolean"):
            return True
        if t in ("list", "array"):
            return [DryStub(effect, f"{path}[0]", None)]
        if t in ("none", "null"):
            return None
    return DryStub(effect, path, None)


def _pb_dry_wrap(value, effect, key, path=""):
    """Wrap a PROVIDED stub so a read it lacks raises DryStubError naming
    the stubs key (plain dicts/lists stay JSON-serializable)."""
    if isinstance(value, dict):
        return _pb_DryDict(
            {k: _pb_dry_wrap(v, effect, key, _pb_dry_key_path(path, k)) for k, v in value.items()},
            effect, key, path,
        )
    if isinstance(value, list):
        return [_pb_dry_wrap(v, effect, key, f"{path}[{i}]") for i, v in enumerate(value)]
    return value


class _pb_DryDict(dict):
    __slots__ = ("_effect", "_key", "_path")

    def __init__(self, items, effect, key, path):
        dict.__init__(self, items)
        self._effect, self._key, self._path = effect, key, path

    def __missing__(self, k):
        path = _pb_dry_key_path(self._path, k)
        raise DryStubError(
            f"dry run: effect {self._effect} has no stubbed value at {_pb_dry_show(path)}; "
            f"the stubs key \"{self._key}\" does not provide it — pass "
            f"stubs={{\"{self._effect}\": {{...{_pb_dry_show(path)}...}}}} "
            f"(or \"{self._effect.rpartition('#')[0]}\" for every occurrence)"
        )


class DryStub:
    """Placeholder for an unstubbed effect result: truthy, iterates ONCE,
    every access is a nested placeholder (or the schema's typed sample);
    str() is `<dry:<id>#<n>.<path>>`; arithmetic raises DryStubError."""

    _dry = True
    __slots__ = ("_effect", "_path", "_schema")

    def __init__(self, effect, path="", schema=None):
        self._effect = effect
        self._path = path
        self._schema = schema if isinstance(schema, dict) else None

    def _error(self, what):
        return DryStubError(
            f"dry run: effect {self._effect} has no stubbed value at "
            f"{_pb_dry_show(self._path)}{what}; pass stubs={{\"{self._effect}\": "
            f"{{...{_pb_dry_show(self._path)}...}}}} (or \"{self._effect.rpartition('#')[0]}\" "
            "for every occurrence)"
        )

    def _child(self, key):
        path = _pb_dry_key_path(self._path, key)
        if self._schema is None:
            return DryStub(self._effect, path, None)
        if key in self._schema:
            return _pb_dry_sample(self._effect, path, self._schema[key])
        raise DryStubError(
            f"dry run: effect {self._effect} has no stubbed value at {_pb_dry_show(path)}; "
            f"{key!r} is not in the declared output schema "
            f"({sorted(self._schema)}); pass stubs={{\"{self._effect}\": "
            f"{{...{_pb_dry_show(path)}...}}}} (or \"{self._effect.rpartition('#')[0]}\" for every occurrence)"
        )

    def __getitem__(self, key):
        if isinstance(key, slice):
            return [DryStub(self._effect, f"{self._path}[0]", None)]
        return self._child(key)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._child(name)

    def __call__(self, *args, **kwargs):
        return DryStub(self._effect, f"{self._path}()", None)

    def get(self, key, default=None):
        if self._schema is not None and key not in self._schema:
            return default
        return self._child(key)

    def keys(self):
        if self._schema is not None:
            return list(self._schema)
        return [f"<dry:{self._effect}{self._path}.keys()[0]>"]

    def values(self):
        return [self._child(k) for k in self.keys()] if self._schema is not None else [
            DryStub(self._effect, f"{self._path}[0]", None)]

    def items(self):
        return list(zip(self.keys(), self.values()))

    def __bool__(self):
        return True

    def __len__(self):
        return 1

    def __iter__(self):
        yield DryStub(self._effect, f"{self._path}[0]", None)

    def __contains__(self, item):
        return True

    def __str__(self):
        return f"<dry:{self._effect}{self._path}>"

    __repr__ = __str__

    def __lt__(self, other):
        return True

    def __le__(self, other):
        return True

    def __gt__(self, other):
        return True

    def __ge__(self, other):
        return True

    # every placeholder equals every other placeholder and no real value:
    # dedup logic (`link not in seen`) terminates instead of growing a work
    # queue forever; ordering compares True so every branch is exercised
    def __hash__(self):
        return hash("<dry>")

    def __eq__(self, other):
        return isinstance(other, DryStub)

    def __ne__(self, other):
        return not self.__eq__(other)

    def _arith(self, other):
        raise self._error(" (arithmetic on a placeholder)")

    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = _arith
    __truediv__ = __rtruediv__ = __floordiv__ = __rfloordiv__ = _arith
    __mod__ = __rmod__ = __pow__ = __rpow__ = __neg__ = _arith

    def __int__(self):
        raise self._error(" (int() of a placeholder)")

    def __float__(self):
        raise self._error(" (float() of a placeholder)")

    __index__ = __int__


def _pb_dry_json_default(value):
    if isinstance(value, DryStub):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
'''

_ns: dict[str, Any] = {}
exec(compile(DRY_CLASSES_SOURCE, "<v2/dry.py>", "exec"), _ns)
DryStub = _ns["DryStub"]
DryStubError = _ns["DryStubError"]
dry_json_default = _ns["_pb_dry_json_default"]


def resolve_stub(stubs: dict[str, Any] | None, effect_id: str, occurrence: int) -> tuple[bool, Any, str | None]:
    """Lookup order `"<id>#<n>"` → `"<id>"` → none. Returns
    (found, value, matched key)."""
    if not stubs:
        return False, None, None
    for key in (f"{effect_id}#{occurrence}", effect_id):
        if key in stubs:
            return True, stubs[key], key
    return False, None, None


def _declared_schema(kind: str, args: dict[str, Any]) -> Any:
    """The output shape the effect declares: `output=` for llm/agent, the
    `returns=` key list for subtask, none for a tool."""
    if kind in ("llm", "agent"):
        out = args.get("output")
        return out if isinstance(out, dict) else None
    if kind == "subtask":
        returns = args.get("returns")
        if isinstance(returns, list) and all(isinstance(k, str) for k in returns):
            return {k: {} for k in returns}
    return None


def dry_answer(
    kind: str, effect_id: str, occurrence: int, stubs: dict[str, Any] | None,
    *, args: dict[str, Any] | None = None, rng: random.Random | None = None,
) -> tuple[Any, dict[str, Any]]:
    """The journaled answer for one effect occurrence in dry mode →
    (result, extra journal fields). `extra` always carries `stubbed`,
    `effect` and `schema`; the jail rebuilds the placeholder from them."""
    args = args or {}
    key = f"{effect_id}#{occurrence}"
    extra: dict[str, Any] = {"stubbed": False, "effect": key, "schema": None, "stub_key": None}
    if kind in STUBBED_KINDS:
        found, value, matched = resolve_stub(stubs, effect_id, occurrence)
        extra["schema"] = _declared_schema(kind, args)
        if found:
            extra["stubbed"] = True
            extra["stub_key"] = matched
            return value, extra
        return None, extra
    if kind == "approve":
        return {
            "approved": True, "request_id": f"dry:{key}", "reason": None,
            "decided_by": None, "dry": True,
        }, extra
    if kind == "wait_event":
        # phase 07: a stub is the event payload the run "received";
        # `{"_event_timeout": true}` answers with the EventTimeout failure
        # (the loop raises it); unstubbed → placeholder, like a tool
        found, value, matched = resolve_stub(stubs, effect_id, occurrence)
        if found:
            extra["stubbed"] = True
            extra["stub_key"] = matched
            if isinstance(value, dict) and value.get("_event_timeout"):
                extra["event_timeout"] = True
                return None, extra
            return value, extra
        return None, extra
    if kind == "now":
        return DRY_NOW, extra
    if kind == "random":
        return (rng or random.Random(0)).random(), extra
    if kind == "log":
        msg = args.get("message")
        return {"message": msg if isinstance(msg, str) else str(msg)}, extra
    raise ValueError(f"effect kind {kind!r} has no dry answer")
