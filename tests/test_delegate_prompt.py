"""plans/020 phase 2 — the delegate prompt is a first-class artifact.

Pins the 11-section shape, the just-in-time reference rule (the 12KB
authoring skill body is never pasted into the delegate's context), the
honesty rules (dry_run simulated, done-as-artifact), and the verbatim tail.
"""

import re

import plugin_playbooks  # noqa: F401 — luna_sdk stub via conftest
from plugin_playbooks import _AUTHORING_SKILL_BODY
from plugin_playbooks.delegation import _PROMPT_TAIL, _delegate_prompt
from plugin_playbooks.models import Playbook


def _pb() -> Playbook:
    return Playbook(
        name="candidate-intake",
        display_name="candidate-intake",
        definition={"name": "candidate-intake", "steps": []},
        status="enabled",
        manifest="INTENT: intake candidates",
    )


def test_eleven_sections_in_order():
    p = _delegate_prompt("fix the phones", _pb())
    headers = re.findall(r"^## (\d+)\.", p, re.M)
    assert headers == [str(i) for i in range(1, 12)]


def test_brief_carries_task_and_manifest():
    p = _delegate_prompt("fix the phones", _pb())
    assert "fix the phones" in p
    assert "INTENT: intake candidates" in p
    assert "candidate-intake" in p
    # From-scratch jobs get no target block.
    p2 = _delegate_prompt("build a digest", None)
    assert "Target playbook" not in p2


def test_reference_fetched_just_in_time_never_pasted():
    p = _delegate_prompt("task", None)
    assert "playbook_language_reference" in p
    assert "FIRST" in p
    # The old prompt pasted the whole authoring skill — the point of the
    # rewrite is that it no longer does. Guard with a distinctive slab.
    slab = _AUTHORING_SKILL_BODY.strip().splitlines()[5]
    assert slab not in p
    assert len(p) < len(_AUTHORING_SKILL_BODY)


def test_honesty_rules_present():
    p = _delegate_prompt("task", None)
    low = p.lower()
    assert "simulated" in low
    assert "never report it as real" in low
    assert "PUBLISHED" in p and "CANDIDATE STOP" in p and "FAILED" in p
    assert "probably fine" in low  # named as the forbidden phrasing
    assert "never work around a refusal" in low


def test_checklist_wired_before_publish():
    # 021: the plans feature is gone — the checklist + approval card are
    # the whole pre-publish story, and the manifest is context, not law.
    p = _delegate_prompt("task", None)
    assert "playbook_plan_write" not in p
    assert "plan_id" not in p
    assert "playbook_manifest_set" in p
    # The checklist sits immediately before the publish instruction.
    assert p.index("Pre-publish checklist") < p.index(
        "playbook_publish(name, explanation=")


def test_budgets_named_with_rationale_and_losing_exits():
    p = _delegate_prompt("task", None)
    assert "40" in p and "15 min" in p
    assert "why:" in p  # the numeric limit carries its rationale
    assert "3 failed validates" in p
    assert "3 failed spec" in p
    assert "likeliest causes" in p  # rank hypotheses before retrying


def test_tail_is_verbatim_and_short():
    p = _delegate_prompt("task", None)
    assert p.rstrip().endswith(_PROMPT_TAIL)
    assert 3 <= len(_PROMPT_TAIL.strip().splitlines()) <= 5


def test_emphasis_stays_scarce():
    # ≤5 shouty lines: emphasis only works when it is rare.
    p = _delegate_prompt("task", None)
    shouty = [
        ln for ln in p.splitlines()
        if len(ln) > 8 and ln == ln.upper() and any(c.isalpha() for c in ln)
    ]
    assert len(shouty) <= 5, shouty


def test_worked_example_is_valid_shape():
    p = _delegate_prompt("task", None)
    assert "playbook(name='digest-open-prs'" in p
    assert "collect=" in p
    assert "BAD" in p and "GOOD" in p
