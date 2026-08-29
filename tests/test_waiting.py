"""plans/013 phase 3 — approval-parking detection.

PARKED is never a status value: it is derived from the event feed (a
prompt_always tool call with no result for longer than the threshold).
The drift test pins `_GATED_TOOLS` to the actual prompt_always ToolDefs in
agent_tools.py so the set cannot rot when a tool's policy changes.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks.delegation import (
    _GATED_TOOLS,
    _WAITING_THRESHOLD_S,
    waiting_on_owner,
)

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


def _ev(label, *, ms, age_s=60.0, kind="tool"):
    ts = (NOW - timedelta(seconds=age_s)).isoformat()
    return {"ts": ts, "phase": "Ship", "kind": kind, "label": label,
            "detail": "", "ms": ms}


def test_pending_gated_call_old_enough_is_waiting():
    events = [_ev("playbook_edit", ms=40), _ev("playbook_publish", ms=None)]
    assert waiting_on_owner(events, now=NOW) == "playbook_publish"


def test_fresh_pending_gated_call_is_not_waiting_yet():
    # A gated call younger than the threshold is probably just executing.
    events = [_ev("playbook_publish", ms=None,
                  age_s=_WAITING_THRESHOLD_S - 1)]
    assert waiting_on_owner(events, now=NOW) is None


def test_resolved_gated_call_is_not_waiting():
    events = [_ev("playbook_publish", ms=812)]
    assert waiting_on_owner(events, now=NOW) is None


def test_pending_ungated_call_is_not_waiting():
    # A slow ordinary tool (spec run) must not read as an approval wait.
    events = [_ev("playbook_spec_run", ms=None, age_s=120)]
    assert waiting_on_owner(events, now=NOW) is None


def test_gated_call_followed_by_later_event_is_not_waiting():
    # Only the LAST event counts — once anything follows, the run moved on.
    events = [_ev("playbook_publish", ms=None),
              _ev("thinking about rollout", ms=None, kind="thought")]
    assert waiting_on_owner(events, now=NOW) is None


def test_empty_or_garbage_events_are_not_waiting():
    assert waiting_on_owner(None, now=NOW) is None
    assert waiting_on_owner([], now=NOW) is None
    assert waiting_on_owner(
        [{"kind": "tool", "label": "playbook_publish", "ms": None,
          "ts": "not-a-date"}], now=NOW) is None


def test_gated_set_matches_prompt_always_tooldefs():
    src = (Path(__file__).parent.parent / "plugin_playbooks"
           / "agent_tools.py").read_text()
    names = [(m.start(), m.group(1))
             for m in re.finditer(r'name="([a-z_]+)"', src)]
    declared = set()
    for m in re.finditer(r'policy="prompt_always"', src):
        prior = [n for pos, n in names if pos < m.start()]
        assert prior, "policy= before any name= — parser assumption broken"
        declared.add(prior[-1])
    assert declared, "no prompt_always ToolDefs found — regex rotted?"
    assert declared == set(_GATED_TOOLS)


def test_payload_prefers_live_feed_over_stale_row():
    # plans/013 phase 3 regression, found on real Luna: the DB flush is
    # throttled, so a delegate that parks right after a gated call leaves
    # the row WITHOUT that last event. The payload must read the live feed.
    import uuid as _uuid
    from plugin_playbooks.delegation import (
        _LIVE_FEEDS, _EventFeed, _delegation_payload,
    )
    from plugin_playbooks.models import PlaybookDelegation

    row = PlaybookDelegation(task="t", status="running", card_token="x" * 10)
    row.id = _uuid.uuid4()
    row.events = [_ev("playbook_edit", ms=40)]  # stale: no promote yet
    row.steps_used = 1

    feed = _EventFeed(None, row.id)
    feed.events = [_ev("playbook_edit", ms=40),
                   _ev("playbook_publish", ms=None, age_s=60)]
    feed.steps_used = 2
    _LIVE_FEEDS[row.id] = feed
    try:
        p = _delegation_payload(row, for_status_tool=True)
        assert p["waiting_for_approval"] == "playbook_publish"
        assert "PAUSED" in p["message"]
        assert p["steps_used"] == 2
        assert p["recent_events"][-1]["label"] == "playbook_publish"
    finally:
        _LIVE_FEEDS.pop(row.id, None)


def test_every_gated_tool_has_owner_words():
    # The PAUSED message speaks owner words, never tool codes — so the map
    # must cover the whole gated set (same drift risk as _GATED_TOOLS).
    from plugin_playbooks.delegation import _GATED_TOOL_OWNER_WORDS
    assert set(_GATED_TOOL_OWNER_WORDS) == set(_GATED_TOOLS)


def test_paused_message_speaks_owner_words_not_tool_codes():
    import uuid as _uuid
    from plugin_playbooks.delegation import (
        _LIVE_FEEDS, _EventFeed, _delegation_payload,
    )
    from plugin_playbooks.models import PlaybookDelegation

    row = PlaybookDelegation(task="t", status="running", card_token="x" * 10)
    row.id = _uuid.uuid4()
    row.events = []
    row.steps_used = 1
    feed = _EventFeed(None, row.id)
    feed.events = [_ev("playbook_publish", ms=None, age_s=60)]
    feed.steps_used = 1
    _LIVE_FEEDS[row.id] = feed
    try:
        p = _delegation_payload(row, for_status_tool=False)
        assert "make the change live" in p["message"]
        assert "playbook_publish" not in p["message"]
    finally:
        _LIVE_FEEDS.pop(row.id, None)
