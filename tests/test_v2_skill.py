"""plans/032 phase 05 — the `playbook-authoring-v2` skill."""

from __future__ import annotations

import re
from pathlib import Path

from plugin_playbooks import _AUTHORING_SKILL_BODY, PlaybooksPlugin
from plugin_playbooks.agent_tools import build_tools
from plugin_playbooks.v2 import MemoryJournalStore
from plugin_playbooks.v2.checker import check
from plugin_playbooks.v2.loop import SegmentLoop
from plugin_playbooks.v2.skill import PUBLISH_RULE, V2_SKILL_BODY, V2_SKILL_MAX_BYTES
from _jail import real_jail, requires_jail
from test_manifest_drift import _Bus, _StubRunner
from test_v2_loop import _pb, _real_tools

_DOC = Path(__file__).resolve().parent.parent / "docs" / "v2.md"
_BLOCK = re.compile(r"```python\n(.*?)```", re.S)

HONESTY = [
    "Never run blind:",
    "Outputs are SIMULATED: NEVER report a dry-run value as a real result.",
    "never re-run a 'running' playbook or invent results.",
    "NEVER report an edit as done after `candidate_saved` — the old version runs until publish succeeds.",
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def test_size_bound():
    assert len(V2_SKILL_BODY.encode("utf-8")) <= V2_SKILL_MAX_BYTES


def _blocks() -> list[str]:
    blocks = _BLOCK.findall(V2_SKILL_BODY)
    assert len(blocks) == 2
    assert blocks == _BLOCK.findall(_DOC.read_text(encoding="utf-8"))
    return blocks


def test_examples_compile():
    for block in _blocks():
        r = check(block, name="doc", version=1)
        assert r.ok, [i.to_dict() for i in r.issues]
        assert not [i for i in r.issues if i.severity == "error"]


@real_jail
@requires_jail()
async def test_examples_compile_and_dry_run(tmp_path):
    loop = SegmentLoop(None, _real_tools(tmp_path), None, None, MemoryJournalStore(keep_completed=True))
    inputs = [{"url": "u", "owner": "o"}, {"urls": ["a"], "owner": "o"}]
    for block, inp in zip(_blocks(), inputs):
        res = await loop.dry_run(_pb("doc", block), inp, {}, version=1)
        assert res["status"] == "simulated", res
        assert res["error"] is None and res["error_type"] is None, res["error"]
        assert res["unreached_call_sites"] == []


def test_honesty_rules_verbatim():
    v2, v1 = _norm(V2_SKILL_BODY), _norm(_AUTHORING_SKILL_BODY)
    for sentence in HONESTY:
        s = _norm(sentence)
        assert s in v2, sentence
        assert s in v1, sentence


def test_loop_v2_order_and_no_validate():
    pos = [V2_SKILL_BODY.index(t) for t in ("playbook_dry_run", "playbook_run_candidate", "playbook_publish")]
    assert pos == sorted(pos)
    assert "do not call `playbook_validate`" in V2_SKILL_BODY
    assert PUBLISH_RULE in V2_SKILL_BODY


def test_registered_next_to_v1():
    skills = {s.name: s for s in PlaybooksPlugin.manifest.skills}
    assert set(skills) == {"playbook-authoring", "playbook-delegation", "playbook-authoring-v2"}
    v2 = skills["playbook-authoring-v2"]
    assert v2.body == V2_SKILL_BODY
    assert set(v2.tools) <= set(PlaybooksPlugin.AUTHORING_TOOLS)
    assert "playbook_language_reference" not in v2.tools
    assert not [t for t in v2.tools if t.startswith("playbook_spec")]
    assert "python" in v2.description and "pblang" in skills["playbook-authoring"].description


def test_publish_rule_in_tool_description():
    tds = {td.name: td for td, _ in build_tools(None, _Bus(), _StubRunner())}
    assert PUBLISH_RULE in tds["playbook_publish"].description
