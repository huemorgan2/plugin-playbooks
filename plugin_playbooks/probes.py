"""Preflight probes — will this playbook's tools work right now? (plans/002 phase 5)

Specs stub the outside world by design, so they can't catch a dead
credential or a vanished resource. Probes can: each tool's owning plugin
may declare a cheap, side-effect-free ``probe`` on its ToolDef (luna plans
038); the preflight engine runs the probes for every tool a playbook
touches and classifies the failures.

Statuses: ``ok`` (probed, works), ``unprobeable`` (tool present, no probe
declared — the common case today), ``failed`` (missing, blocked, or the
probe said no). Only ``failed`` blocks a publish.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .models import Playbook, PlaybookProbeResult

FAILURE_CLASSES = (
    "tool_missing", "blocked", "credential_dead", "resource_gone",
    "permission", "rate_limited", "unknown",
)


def collect_tools(definition: dict[str, Any]) -> list[str]:
    """Every tool name a definition can touch: tool_call steps plus
    agent-step allowlists, recursing into condition branches, loop bodies,
    and parallel branches. Sorted, deduped. Subtask targets are collected
    separately (see collect_subtasks) — their tools belong to their own
    definitions."""
    found: set[str] = set()

    def walk(steps: list[dict[str, Any]]) -> None:
        for step in steps or []:
            if step.get("tool"):
                found.add(step["tool"])
            if step.get("kind") == "code":
                # plans/004: code steps delegate to plugin-inline-code-run —
                # advertise the dependency so preflight/publish gate on it.
                found.add("code_run")
            for name in step.get("tools") or []:
                found.add(name)
            for key in ("then", "else", "body"):
                if step.get(key):
                    walk(step[key])
            for branch in step.get("branches") or []:
                walk(branch)

    walk(definition.get("steps") or [])
    return sorted(found)


def collect_subtasks(definition: dict[str, Any]) -> list[str]:
    """Names of playbooks referenced by subtask steps, anywhere in the IR."""
    found: set[str] = set()

    def walk(steps: list[dict[str, Any]]) -> None:
        for step in steps or []:
            if step.get("kind") == "subtask" and step.get("playbook"):
                found.add(step["playbook"])
            for key in ("then", "else", "body"):
                if step.get(key):
                    walk(step[key])
            for branch in step.get("branches") or []:
                walk(branch)

    walk(definition.get("steps") or [])
    return sorted(found)


async def probe_tool(registry: Any, name: str) -> dict[str, Any]:
    """Probe one tool. Never raises — a broken probe is a failed probe."""
    if registry is None:
        return {"tool": name, "plugin": None, "status": "unprobeable",
                "failure_class": None, "detail": "no tool registry available"}
    try:
        rt = registry.get(name)
    except KeyError:
        return {"tool": name, "plugin": None, "status": "failed",
                "failure_class": "tool_missing",
                "detail": "tool is not registered — plugin missing or renamed"}
    plugin = getattr(rt, "plugin", None)
    definition = getattr(rt, "definition", None)
    policy_fn = getattr(definition, "effective_policy", None)
    if callable(policy_fn) and policy_fn() == "block":
        return {"tool": name, "plugin": plugin, "status": "failed",
                "failure_class": "blocked",
                "detail": "tool policy is 'block' — it can never fire"}
    # duck-typed: older cores have no `probe` field on ToolDef at all.
    probe = getattr(definition, "probe", None)
    if probe is None:
        return {"tool": name, "plugin": plugin, "status": "unprobeable",
                "failure_class": None,
                "detail": "tool present; owning plugin declares no probe"}
    try:
        if getattr(probe, "handler", None) is not None:
            out = await probe.handler()
        elif getattr(probe, "args", None) is not None:
            out = await rt.handler(**probe.args)
        else:
            return {"tool": name, "plugin": plugin, "status": "unprobeable",
                    "failure_class": None,
                    "detail": "probe declares neither handler nor args"}
    except Exception as e:  # noqa: BLE001
        return {"tool": name, "plugin": plugin, "status": "failed",
                "failure_class": "unknown", "detail": f"probe raised: {e}"}
    if isinstance(out, dict) and out.get("ok") is False:
        fc = out.get("failure_class") or "unknown"
        if fc not in FAILURE_CLASSES:
            fc = "unknown"
        return {"tool": name, "plugin": plugin, "status": "failed",
                "failure_class": fc, "detail": str(out.get("detail") or "")}
    return {"tool": name, "plugin": plugin, "status": "ok",
            "failure_class": None,
            "detail": str((out or {}).get("detail") or "") if isinstance(out, dict) else ""}


async def run_preflight(
    session: Any,
    registry: Any,
    playbook: Any,
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Probe every tool `definition` touches (following subtask targets one
    level deep via their LIVE definitions) and upsert the per-tool cache
    rows for `playbook`. Caller commits."""
    tools = set(collect_tools(definition))
    for sub_name in collect_subtasks(definition):
        sub = (await session.execute(
            select(Playbook).where(Playbook.name == sub_name)
        )).scalar_one_or_none()
        if sub is not None:
            tools.update(collect_tools(sub.definition or {}))

    results = [await probe_tool(registry, name) for name in sorted(tools)]

    now = datetime.now(timezone.utc)
    existing = {
        row.tool: row for row in (await session.execute(
            select(PlaybookProbeResult).where(
                PlaybookProbeResult.playbook_id == playbook.id
            )
        )).scalars().all()
    }
    for res in results:
        row = existing.get(res["tool"])
        if row is None:
            row = PlaybookProbeResult(playbook_id=playbook.id, tool=res["tool"])
            session.add(row)
        row.status = res["status"]
        row.failure_class = res["failure_class"]
        row.detail = res["detail"]
        row.probed_at = now
    # tools no longer referenced: drop their stale rows
    for tool, row in existing.items():
        if tool not in {r["tool"] for r in results}:
            await session.delete(row)

    counts = {"ok": 0, "unprobeable": 0, "failed": 0}
    for res in results:
        counts[res["status"]] += 1
    return {"total": len(results), **counts, "results": results}


async def reprobe_enabled(session_factory: Any, registry: Any) -> list[dict[str, Any]]:
    """Daily sweep body: preflight every enabled playbook and return the
    NEW failures — tools that flipped into `failed` since the last probe.
    (Already-failed tools stay silent; the alert fired when they broke.)
    Pure of scheduling and notification so tests can call it directly."""
    alerts: list[dict[str, Any]] = []
    async with session_factory() as session:
        playbooks = (await session.execute(
            select(Playbook).where(Playbook.status == "enabled")
        )).scalars().all()
        for pb in playbooks:
            prev_failed = {
                row.tool for row in (await session.execute(
                    select(PlaybookProbeResult).where(
                        PlaybookProbeResult.playbook_id == pb.id,
                        PlaybookProbeResult.status == "failed",
                    )
                )).scalars().all()
            }
            summary = await run_preflight(
                session, registry, pb, pb.definition or {},
            )
            for res in summary["results"]:
                if res["status"] == "failed" and res["tool"] not in prev_failed:
                    alerts.append({"playbook": pb.name, **res})
        await session.commit()
    return alerts


def preflight_note(summary: dict[str, Any]) -> str:
    """Gate-note wording: '2 ok · 3 unprobeable' / 'no tools'."""
    if not summary["total"]:
        return "no tools"
    parts = [f"{summary[k]} {k}" for k in ("ok", "unprobeable", "failed") if summary[k]]
    return " · ".join(parts)
