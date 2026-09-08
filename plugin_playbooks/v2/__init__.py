"""v2 runtime — shared constants (plans/032 phase 01) and, since phase 02,
the runtime exports (`SegmentLoop`, `JournalStore`, `MemoryJournalStore`,
`SHIM_SOURCE`) at the bottom.

The constants come first and import nothing: `docs/v2.md` is the contract
text, the checker (`plugin_playbooks.v2.checker`), the shim/segment loop
and the skill (phase 05) are all written from it and read these values so
the doc and the code cannot drift. The runtime modules import the constants
from this package, so they are imported LAST.
"""

from __future__ import annotations

# Replay cost is O(effects^2) in pure compute — hard cap per run (docs/v2.md §12).
MAX_EFFECTS = 200

# Playbook code formats accepted by `resolve_format` (docs/v2.md §9).
FORMATS = ("pblang", "python")

# `ctx` effects the checker admits at this phase (docs/v2.md §2).
AVAILABLE_EFFECTS = frozenset({
    "tool", "llm", "agent", "subtask", "gather", "approve", "now", "random", "log",
})

# Effects the doc names but this version does not run — rejected by rule
# `v2-effect-unavailable` with exactly this message text (docs/v2.md §2, §8).
UNAVAILABLE_EFFECTS = {
    "wait_event": "not available in this version",
    "sleep": "not available in this version",
}

# Feature flags handed to `check(features=...)`; phase 07 adds "wait_event".
DEFAULT_FEATURES = frozenset()

# Exception classes exposed on `ctx` (docs/v2.md §4).
CTX_EXCEPTIONS = frozenset({
    "EffectError", "ToolError", "EffectTimeout", "OutcomeUnknown", "Rejected",
    "ApprovalExpired", "EventTimeout", "SubtaskFailed",
})
# Derive from BaseException — cannot be caught in a playbook (docs/v2.md §4).
CTX_UNCATCHABLE = frozenset({"RunCancelled", "JournalDivergence", "MaxEffectsExceeded"})

# Default `_timeout` per effect kind, seconds; None = unbounded (docs/v2.md §12).
DEFAULT_TIMEOUTS = {"tool": 120, "llm": 300, "agent": 900, "subtask": None}

# The decided `ctx.approve` result shape (docs/v2.md §2). The in-process form
# (phase 03), the park form (phase 07) and the dry answer (phase 05, which adds
# `dry`) return exactly these keys. `approval_id` is NOT one of them — that
# name is reserved for the `parked_on` record.
APPROVE_RESULT_KEYS = frozenset({"approved", "request_id", "reason", "decided_by"})

# Runtime exports (phase 02) — after the constants, which these modules import.
from .journal import JournalStore, MemoryJournalStore  # noqa: E402
from .journal_db import DbJournalStore  # noqa: E402
from .loop import SegmentLoop  # noqa: E402
from .shim import SHIM_SOURCE  # noqa: E402

__all__ = [
    "MAX_EFFECTS", "FORMATS", "AVAILABLE_EFFECTS", "UNAVAILABLE_EFFECTS",
    "DEFAULT_FEATURES", "CTX_EXCEPTIONS", "CTX_UNCATCHABLE", "DEFAULT_TIMEOUTS",
    "APPROVE_RESULT_KEYS", "SegmentLoop", "JournalStore", "MemoryJournalStore",
    "DbJournalStore", "SHIM_SOURCE",
]
