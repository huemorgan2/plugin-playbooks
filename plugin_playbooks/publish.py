"""0.26.0 (plans/015, 089): the publish contract — shared helpers.

Plans 089 contract #8: publish is a distinct function whose preconditions are
machine-checked in the handler — never LLM discretion. This module holds the
pieces every publish path (agent tool, HTTP route) shares, so no code path
makes a version live without passing the gate:

- feature-detected core accessors (`ops_conversation_id`,
  `conversation_kind`, `conversation_state`) that return None on cores
  without the 089 P1–P3 surfaces instead of raising;
- the test-run gate: a version may go live only with a green run of that
  EXACT version recorded after the version snapshot was created (version
  rows are immutable — every edit mints a new number, so "since last edit"
  is simply "after the version row's created_at");
- the ops-chat announcement + `playbook.published` bus event every publish
  and rollback must produce.
"""
from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PlaybookRun, PlaybookStepRun

log = logging.getLogger("luna.plugin.playbooks.publish")


# --- feature-detected core accessors (089 core P1–P3) -----------------------

def _call_accessor(ctx: Any, name: str) -> Any | None:
    fn = getattr(ctx, name, None)
    if fn is None:
        return None
    try:
        value = fn() if callable(fn) else fn
    except Exception:  # noqa: BLE001 — absent/broken accessor == old core
        return None
    if inspect.isawaitable(value):
        # An async accessor we can't await from a sync helper — treat the
        # surface as absent rather than leak a pending coroutine.
        value.close()
        return None
    return value


def ops_conversation_id(ctx: Any) -> Any | None:
    """The ops chat's id, or None on cores without one (pre-089 P1)."""
    if ctx is None:
        return None
    return _call_accessor(ctx, "ops_conversation_id")


def conversation_kind(ctx: Any) -> str | None:
    """'building' | 'ops' for the current turn's chat; None when headless
    or on cores without 089 P3."""
    if ctx is None:
        return None
    value = _call_accessor(ctx, "conversation_kind")
    return value if isinstance(value, str) else None


def conversation_state(ctx: Any) -> str | None:
    """planning/building/identify/fix_approve/fix_publish, or None."""
    if ctx is None:
        return None
    value = _call_accessor(ctx, "conversation_state")
    return value if isinstance(value, str) else None


# --- the test-run gate ------------------------------------------------------

async def latest_run_evidence(
    session: AsyncSession,
    playbook_id: Any,
    version: int,
    since: datetime | None,
    *,
    include_live: bool = False,
) -> PlaybookRun | None:
    """The most recent COMPLETED run of exactly `version` started after
    `since`. By default only test runs count (`is_test`, plus the pre-0.26
    'agent-candidate' trigger stamp so upgrades don't orphan fresh evidence);
    `include_live=True` also accepts production runs — used when restoring a
    previously-live version, whose live history IS its evidence."""
    q = (
        select(PlaybookRun)
        .where(
            PlaybookRun.playbook_id == playbook_id,
            PlaybookRun.playbook_version == version,
            PlaybookRun.status.in_(("done", "failed")),
        )
        .order_by(PlaybookRun.started_at.desc())
        .limit(1)
    )
    if not include_live:
        q = q.where(or_(
            PlaybookRun.is_test.is_(True),
            PlaybookRun.trigger == "agent-candidate",
        ))
    if since is not None:
        q = q.where(PlaybookRun.started_at > since)
    return (await session.execute(q)).scalar_one_or_none()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def test_run_gate(
    session: AsyncSession,
    playbook_id: Any,
    version: int,
    edited_at: datetime | None,
    *,
    include_live: bool = False,
) -> tuple[dict[str, Any], str | None, PlaybookRun | None]:
    """Machine-checked precondition #2 of the publish contract.

    Returns (gate_entry, refusal_json_or_None, evidence_run). The refusal
    states exactly what's missing so the agent can go do it instead of
    arguing with the gate.
    """
    run = await latest_run_evidence(
        session, playbook_id, version, _aware(edited_at),
        include_live=include_live,
    )
    if run is None:
        gate = {
            "gate": "test_run", "ok": False,
            "note": f"version {version} has no test run since its last edit",
        }
        refusal = json.dumps({
            "error": (
                "Publish refused — gate 'test_run' failed: version "
                f"{version} has not been tested since its last edit."
            ),
            "gate": "test_run",
            "hint": (
                "Run it as a test first — playbook_run_candidate for the "
                "candidate (real run, real side effects) — and publish "
                "again once it completes green."
            ),
        })
        return gate, refusal, None
    if run.status != "done":
        # run rows carry no error text — the failed step does.
        step_error = (await session.execute(
            select(PlaybookStepRun.error).where(
                PlaybookStepRun.run_id == run.id,
                PlaybookStepRun.status == "failed",
            ).limit(1)
        )).scalar_one_or_none()
        gate = {
            "gate": "test_run", "ok": False,
            "note": f"latest test of version {version} FAILED (run {run.id})",
        }
        refusal = json.dumps({
            "error": (
                "Publish refused — gate 'test_run' failed: the latest test "
                f"run of version {version} FAILED."
            ),
            "gate": "test_run",
            "run_id": str(run.id),
            "run_error": step_error,
            "hint": (
                "Fix the playbook (playbook_edit) or the failing tool, run "
                "the test again, and publish once it's green."
            ),
        })
        return gate, refusal, run
    gate = {
        "gate": "test_run", "ok": True,
        "note": (
            f"green run {run.id} of version {version} at "
            f"{run.completed_at.isoformat() if run.completed_at else '?'}"
        ),
        "run_id": str(run.id),
    }
    return gate, None, run


# --- announcement + bus event -----------------------------------------------

async def announce_publish(
    ctx: Any,
    events: Any,
    *,
    name: str,
    old_version: int | None,
    new_version: int,
    evidence: PlaybookRun | None,
    actor: str,
    action: str = "publish",
    summary: str = "",
) -> None:
    """Contract #8: every publish (and rollback) is announced in the ops
    chat with version + test evidence, and emits `playbook.published`.
    Never raises — announcement failure must not undo a publish."""
    payload: dict[str, Any] = {
        "name": name,
        "action": action,
        "old_version": old_version,
        "new_version": new_version,
        "actor": actor,
    }
    if evidence is not None:
        payload["evidence_run_id"] = str(evidence.id)
    try:
        await events.emit("playbook.published", payload)
    except Exception:  # noqa: BLE001
        log.exception("playbook.published emit failed name=%s", name)

    if ctx is None:
        return
    send = getattr(ctx, "send_muted_message", None)
    if send is None:
        return
    target = ops_conversation_id(ctx)
    if target is None:
        # Pre-089 core: no ops chat. The current conversation (when there is
        # one) still hears about it via the tool result; skip the muted note.
        log.info(
            "playbook.publish announce skipped (no ops chat) name=%s v%s→v%s",
            name, old_version, new_version,
        )
        return
    verb = "rolled back to" if action == "rollback" else "published"
    lines = [
        f"Playbook `{name}` {verb} version {new_version}"
        + (f" (was {old_version})" if old_version else "") + f" by {actor}.",
    ]
    if summary:
        lines.append(summary)
    if evidence is not None:
        when = evidence.completed_at.isoformat() if evidence.completed_at else "?"
        lines.append(
            f"Test evidence: green run {evidence.id} of version "
            f"{new_version} at {when}."
        )
    else:
        lines.append("Test evidence: prior live history of this version.")
    try:
        await send(
            f"Playbook {verb}: {name}",
            "\n".join(lines),
            channel="awareness",
            respond=False,
            conversation_id=target,
            source="playbooks",
        )
    except TypeError:
        # Older send_muted_message signature — retry with the minimal form.
        try:
            await send(f"Playbook {verb}: {name}", "\n".join(lines))
        except Exception:  # noqa: BLE001
            log.exception("publish announce failed name=%s", name)
    except Exception:  # noqa: BLE001
        log.exception("publish announce failed name=%s", name)
