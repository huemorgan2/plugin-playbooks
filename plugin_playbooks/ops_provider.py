"""0.31.0 (plans/019): playbooks as an ops provider.

plugin-ops owns the generic self-maintenance machinery (problem ledger,
plan-level approvals, negotiation, disk record — its plans/001). Playbooks
is one provider of fixable area: it reports problems and outcomes on the
core event bus and lets plugin-ops' plan scope gate its own mutation tools.

Everything here degrades: without plugin-ops (provider key "ops" absent
from the registry) every function is a no-op / None and the 0.30.x
behavior stands.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("luna.playbooks.ops_provider")


def ops_authority(ctx: Any) -> Any | None:
    """The plugin-ops provider object, or None when plugin-ops isn't loaded
    (or the core predates the provider registry)."""
    registry = getattr(ctx, "provider_registry", None)
    if registry is None:
        return None
    try:
        if not registry.has("ops"):
            return None
        return registry.get("ops")
    except Exception:  # noqa: BLE001 — a broken registry must not break tools
        log.exception("ops provider lookup failed")
        return None


async def report_problem(
    ctx: Any,
    events: Any,
    *,
    name: str,
    signature: str,
    display_name: str,
    purpose: str,
    run_id: str,
    version: Any,
    step_id: str,
    error: str,
) -> bool:
    """Emit `ops.problem_reported` for a live-run failure. Returns True when
    plugin-ops is present and the emit happened (the caller must then NOT
    raise its own fix-proposal card); False → caller falls back to 0.30.x.

    Repeats are reported too: plugin-ops dedupes by (provider, signature),
    bumps its counter, and refreshes the evidence — the freshest failure is
    the one worth diagnosing.
    """
    if ops_authority(ctx) is None:
        return False
    payload = {
        "provider": "plugin-playbooks",
        "area_ref": f"playbook:{name}",
        "signature": signature,
        "evidence": {
            "run_id": run_id,
            "version": version,
            "step": step_id,
            "error": (error or "")[:400],
        },
        "display": {"name": display_name, "purpose": purpose},
    }
    try:
        await events.emit("ops.problem_reported", payload)
    except Exception:  # noqa: BLE001 — reporting must not break the run path
        log.exception("ops.problem_reported emit failed name=%s", name)
        return False
    log.info("ops.problem_reported name=%s sig=%s", name, signature[:12])
    return True


async def scope_refusal(ctx: Any, name: str) -> str | None:
    """Tool-layer plan-scope gate (PLAN.md #3). Returns a refusal JSON string
    when this mutation must not touch playbook `name`, else None.

    Applies ONLY inside a kind-`ops` conversation with an active plan whose
    scope is `plan_only`: the owner approved the changes IN THE PLAN, and the
    plan's `targets` list is the whole authorization. `anything_needed`, no
    active plan, or any other conversation → unchanged behavior.
    """
    try:
        kind = ctx.conversation_kind() if ctx is not None else None
    except Exception:  # noqa: BLE001 — headless ctx fakes
        kind = None
    if kind != "ops":
        return None
    authority = ops_authority(ctx)
    if authority is None:
        return None
    try:
        plan = await authority.active_plan()
    except Exception:  # noqa: BLE001 — a broken query must not brick tools
        log.exception("ops active_plan query failed")
        return None
    if not isinstance(plan, dict):
        return None
    if plan.get("scope") != "plan_only":
        return None
    target = f"playbook:{name}"
    targets = [str(t) for t in (plan.get("targets") or [])]
    if target in targets:
        return None
    return json.dumps({
        "error": (
            f"Refused — '{target}' is outside the approved plan. The owner "
            "approved the changes in the plan, and that plan declares "
            f"targets {targets}. Nothing was changed."
        ),
        "gate": "ops_plan_scope",
        "plan_id": plan.get("plan_id"),
        "hint": (
            "If the fix genuinely needs this target, file a revised plan "
            "with ops_file_plan naming it, and let the owner approve that. "
            "Do not work around this gate."
        ),
    })


async def report_outcome(
    ctx: Any,
    events: Any,
    *,
    name: str,
    facts: dict[str, Any],
) -> None:
    """Emit `ops.outcome` after a successful gated publish (PLAN.md #4).

    Only from a kind-`ops` conversation with plugin-ops present: plugin-ops
    matches outcomes plan_id → area_ref → single-executing, so a routine
    building-chat publish must never close an executing plan by accident.
    Never raises — an outcome emit must not undo a publish.
    """
    try:
        kind = ctx.conversation_kind() if ctx is not None else None
    except Exception:  # noqa: BLE001
        kind = None
    if kind != "ops":
        return
    if ops_authority(ctx) is None:
        return
    try:
        await events.emit("ops.outcome", {
            "area_ref": f"playbook:{name}",
            "facts": facts,
        })
        log.info("ops.outcome emitted name=%s", name)
    except Exception:  # noqa: BLE001
        log.exception("ops.outcome emit failed name=%s", name)
