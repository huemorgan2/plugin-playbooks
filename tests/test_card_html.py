"""plans/013 phase 2 — the srcdoc card document itself."""

import json

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks.card import render_delegation_card


def _card(playbook="candidate-intake"):
    return render_delegation_card(
        "11111111-2222-3333-4444-555555555555", "tok-abc", playbook, "0.24.0"
    )


def test_id_and_token_baked_in():
    doc = _card()
    assert doc.count("11111111-2222-3333-4444-555555555555") == 1
    assert doc.count("tok-abc") == 1
    boot = json.loads(doc.split("var BOOT=")[1].split(";\n")[0])
    assert boot["token"] == "tok-abc"
    assert boot["phases"] == ["Understand", "Change", "Prove", "Ship"]
    assert boot["pollMs"] == 1500


def test_self_contained_no_external_resources():
    doc = _card()
    # The only network touch is the relative poll path — no absolute URLs,
    # no external scripts/styles/fonts (the iframe may be CSP-restricted).
    assert "http://" not in doc and "https://" not in doc
    assert "<link" not in doc and "src=" not in doc
    assert "/api/p/plugin-playbooks/delegations/" in doc
    assert "luna:embed:height" in doc  # chat-ui auto-size contract


def test_phase_labels_and_states_present():
    doc = _card()
    for label in ("Understand", "Change", "Prove", "Ship"):
        assert label in doc
    for state in ("done", "failed", "needs_owner"):
        assert state in doc


def test_hostile_playbook_name_is_escaped():
    doc = _card(playbook='<script>alert(1)</script>"pwn')
    assert "<script>alert" not in doc
    # The name reaches JS only through json.dumps, which escapes the tag.
    assert "<\\/script>" in doc or "\\u003c" in doc


def test_no_playbook_means_no_chip():
    assert '<span class="chip">' not in _card(playbook="")
    assert '<span class="chip">' in _card()


def test_waiting_banner_wiring_present():
    # plans/013 phase 3 — the parked-state banner: element, owner-words map,
    # and the render keyed on waiting_for_approval.
    doc = _card()
    assert 'id="waiting"' in doc
    assert "waiting_for_approval" in doc
    assert "Waiting for your approval" in doc
    # Owner words, not tool codes, reach the reader.
    assert "make the change live" in doc
    assert "roll back the live version" in doc


def test_poll_url_uses_hosted_base_prefix():
    # plans/014 — hosted tenants live under /a/{slug}; a root-relative fetch
    # leaves the tenant. The card must compute API_BASE and prepend it.
    doc = _card()
    assert "var API_BASE=" in doc
    assert "document.baseURI" in doc
    assert r"(\/a\/[^\/]+)" in doc
    assert "fetch(API_BASE+'/api/p/plugin-playbooks/delegations/'" in doc
    # No bare root-relative fetch may remain.
    assert "fetch('/api" not in doc


def test_auth_blocked_poll_shows_honest_offline_state():
    # plans/014 — the sandboxed iframe has no credentials, so a proxy 401/403
    # can never heal: stop polling and say so instead of "Connection lost".
    doc = _card()
    assert "r.status===401||r.status===403" in doc
    assert "offline()" in doc
    assert "live updates can\\'t show here" in doc
    # CORS-less proxy errors surface as network failures — the fallback must
    # trip after the first polls all fail.
    assert "!everPolled&&failedPolls>=5" in doc


# ── plans/020 phase 3 — the card the owner actually watches ──────────────


def test_feed_is_open_by_default():
    # The whole point of the refresh: execution visible without a click.
    assert '<details id="detail" open>' in _card()


def test_tool_rows_carry_verdict_ticks():
    doc = _card()
    # ✓ for ok, ✗ for a failed call, pulsing dot while in flight.
    assert 'class="tick ok"' in doc and "✓" in doc
    assert 'class="tick err"' in doc and "✗" in doc
    assert 'class="tick run"' in doc
    assert "e.ok===false" in doc  # keyed on the phase-2 ok field
    assert "e.ms==null" in doc    # in-flight = no duration yet


def test_args_hint_rendered_and_escaped():
    doc = _card()
    assert "function hintFor" in doc
    assert "e.args" in doc
    # Hints reach the DOM only through esc() — never raw innerHTML concat.
    assert "esc(hint)" in doc
    # The scrub sentinel for secret values is skipped, not displayed.
    assert "\\u2022\\u2022\\u2022" in doc


def test_steps_budget_counter():
    doc = _card()
    boot = json.loads(doc.split("var BOOT=")[1].split(";\n")[0])
    assert boot["maxSteps"] == 40
    assert "st.steps_used+'/'+BOOT.maxSteps" in doc


def test_max_steps_matches_delegation_budget():
    # card._MAX_STEPS mirrors delegation._MAX_TURNS (no import: delegation
    # imports card). This is the drift pin.
    from plugin_playbooks import card as card_mod
    from plugin_playbooks import delegation
    assert card_mod._MAX_STEPS == delegation._MAX_TURNS


def test_autoscroll_respects_reader_position():
    doc = _card()
    assert "feed.scrollHeight-feed.scrollTop-feed.clientHeight<24" in doc
    assert "if(pinned)feed.scrollTop=feed.scrollHeight" in doc


def test_terminal_states_color_the_top_border():
    doc = _card()
    for state in ("done", "failed", "needs_owner"):
        assert f".card.{state}{{border-top-color:" in doc
