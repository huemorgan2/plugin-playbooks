"""Result provenance (plans/032 phase 09; master §2 "Result provenance").

Every run-shaped tool result opens with the same five keys so a reader — the
agent, a grader, an owner looking at the raw tool output — knows WHAT it is
before it reads anything else:

    kind          real_run | candidate_test_run | dry_run
    side_effects  true for the two real kinds, false for a dry run
    version       the number of the content that ran (the run row's
                  `playbook_version`; the resolved target for a dry run)
    version_role  live | candidate | historical — what that number was to
                  the playbook when the result was produced
    run_id        the run row's id; null for a dry run (no row is written)

Format-agnostic: v1 pblang and v2 python results carry the same envelope,
so this sits beside agent_tools.py, not under `v2/`. Refusals, errors and
non-run results (`playbook_overview` itself) carry no envelope.
"""

from __future__ import annotations

from typing import Any

KINDS = ("real_run", "candidate_test_run", "dry_run")
ROLES = ("live", "candidate", "historical")
ENVELOPE_KEYS = ("kind", "side_effects", "version", "version_role", "run_id")


def envelope(
    kind: str, *, side_effects: bool, version: int | None,
    version_role: str, run_id: str | None,
) -> dict[str, Any]:
    """The five provenance keys, in contract order."""
    if kind not in KINDS:
        raise ValueError(f"unknown provenance kind {kind!r} (expected one of {KINDS})")
    if version_role not in ROLES:
        raise ValueError(f"unknown version role {version_role!r} (expected one of {ROLES})")
    return {
        "kind": kind,
        "side_effects": bool(side_effects),
        "version": version,
        "version_role": version_role,
        "run_id": run_id,
    }


def with_envelope(env: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """A fresh dict: the five envelope keys first, then `body`'s keys in
    their existing order. A body key that repeats an envelope key (`run_id`)
    keeps the envelope's position and takes the body's value."""
    out: dict[str, Any] = {k: env[k] for k in ENVELOPE_KEYS}
    for k, v in body.items():
        out[k] = v
    return out


def row_provenance(*, is_test: Any, trigger: Any) -> tuple[str, str]:
    """(kind, version_role) for a run ROW — derived from what was stamped at
    creation, never from the playbook's current pointers: a candidate test
    run of v3 still reports `candidate_test_run` after v3 is promoted. The
    rule is the one `publish.latest_run_evidence` uses."""
    if bool(is_test) or trigger == "agent-candidate":
        return "candidate_test_run", "candidate"
    return "real_run", "live"


def row_envelope(run: Any) -> dict[str, Any]:
    """Envelope for a `PlaybookRun` row (or anything with its attributes)."""
    kind, role = row_provenance(
        is_test=getattr(run, "is_test", False), trigger=getattr(run, "trigger", None),
    )
    return envelope(
        kind, side_effects=True, version=getattr(run, "playbook_version", None),
        version_role=role, run_id=str(run.id),
    )


def overview_hint(name: str) -> str:
    """The pointer every `next` hint ends with."""
    return (
        f"playbook_overview(name='{name}') is the truth surface — read it "
        "before describing this playbook's state."
    )
