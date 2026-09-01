"""0.26.0 (plans/015, 089): the publish contract — shared helpers.

Plans 089 contract #8: publish is a distinct function whose preconditions are
machine-checked in the handler — never LLM discretion. This module holds the
pieces every publish path (agent tool, HTTP route) shares, so no code path
makes a version live without passing the gate:

- thin wrappers over the core 089 P1–P3 accessors (`ops_conversation_id`,
  `conversation_kind`, `conversation_state`) that tolerate a missing ctx
  (headless tests) but otherwise call the shipped SDK directly;
- the test-run gate: a version may go live only with a green run of that
  EXACT version recorded after the version snapshot was created (version
  rows are immutable — every edit mints a new number, so "since last edit"
  is simply "after the version row's created_at");
- the ops-chat announcement + `playbook.published` bus event every publish
  and rollback must produce.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PlaybookRun, PlaybookStepRun

log = logging.getLogger("luna.plugin.playbooks.publish")


# --- core 089 accessors (real on luna >= 0.87.003) --------------------------

async def ops_conversation_id(ctx: Any) -> Any | None:
    """The singleton ops chat's id (`await ctx.ops_conversation_id()`,
    get-or-create). None only with no ctx, or if the lookup itself fails —
    delivery seams degrade to their origin fallback rather than raising."""
    if ctx is None:
        return None
    try:
        return await ctx.ops_conversation_id()
    except Exception:  # noqa: BLE001 — a broken DB must not undo the caller
        log.exception("ops_conversation_id lookup failed")
        return None


def conversation_kind(ctx: Any) -> str | None:
    """'building' | 'ops' for the current turn's chat; None when headless."""
    if ctx is None:
        return None
    return ctx.conversation_kind()


def conversation_state(ctx: Any) -> str | None:
    """planning/building/identify/fix_approve/fix_publish; None headless."""
    if ctx is None:
        return None
    return ctx.conversation_state()


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


_RELAX = "Owner can relax this in Settings → Publish."


async def test_run_gate(
    session: AsyncSession,
    playbook_id: Any,
    version: int,
    edited_at: datetime | None,
    *,
    include_live: bool = False,
    require: bool = True,
) -> tuple[dict[str, Any], str | None, PlaybookRun | None]:
    """Machine-checked precondition #2 of the publish contract.

    Returns (gate_entry, refusal_json_or_None, evidence_run). The refusal
    states exactly what's missing so the agent can go do it instead of
    arguing with the gate.
    """
    # 0.27.1 (plans/016 phase 1): for restores/rollbacks the `since` bound
    # is dropped. Version rows are immutable, so ANY completed run of exactly
    # this version ran exactly this content — and snapshot rows are minted at
    # the NEXT edit ("before whole-YAML edit", `_ensure_live_row`), so their
    # created_at post-dates every run the version ever had. Keeping the
    # bound made every owner restore fail with "not tested since its last
    # edit" (silently, in the UI). Candidates keep the strict rule: they are
    # the one case where "since the row was created" means "since the edit".
    since = None if include_live else _aware(edited_at)
    run = await latest_run_evidence(
        session, playbook_id, version, since, include_live=include_live,
    )
    if run is None:
        if include_live:
            note = f"version {version} has never completed a run"
            error = (
                "Publish refused — gate 'test_run' failed: version "
                f"{version} has never completed a run, so there is no "
                "evidence it works."
            )
            hint = (
                "Run this version once (a test run is enough) and restore "
                "it again once that run completes green."
            )
        else:
            note = f"version {version} has no test run since its last edit"
            # plans/016 phase 3: green specs routinely precede this refusal
            # and read as "all tests pass" — say why they are not enough.
            error = (
                "Publish refused — gate 'test_run' failed: version "
                f"{version} has not had a REAL run since its last edit. "
                "Passing specs are not run evidence — specs are dry-run "
                "simulations with tools stubbed."
            )
            hint = (
                "Run the candidate for real once — playbook_run_candidate "
                "(real tools, real side effects) — and publish again when "
                "it completes green."
            )
        gate = {"gate": "test_run", "ok": False, "note": note}
        if not require:
            # plans/016 phase 6: reported, never refused (Settings → Publish)
            gate["enforced"] = False
            gate["note"] += " — not enforced (Settings → Publish)"
            return gate, None, None
        error += " " + _RELAX
        refusal = json.dumps({"error": error, "gate": "test_run", "hint": hint})
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
        if not require:
            gate["enforced"] = False
            gate["note"] += " — not enforced (Settings → Publish)"
            return gate, None, run
        refusal = json.dumps({
            "error": (
                "Publish refused — gate 'test_run' failed: the latest test "
                f"run of version {version} FAILED. " + _RELAX
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
    target = await ops_conversation_id(ctx)
    if target is None:
        # Ops lookup failed (broken DB). The current conversation (when there
        # is one) still hears about it via the tool result; skip the note.
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
    except Exception:  # noqa: BLE001
        log.exception("publish announce failed name=%s", name)


async def specs_gate(
    session: AsyncSession,
    runner: Any,
    playbook_id: Any,
    target: Any,
    version_n: int,
    *,
    require: bool = True,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """plans/016 phase 5: the specs gate, evaluated on the specs OF the
    version going live (`playbook_specs.playbook_version == version_n`)
    against that version's content — candidates AND restores/rollbacks
    (supersedes plans/015 deviation #4). Returns (gate_entry, refusal_dict);
    the refusal is None when every spec passed or the version has none.
    Spec result caches are updated on the rows — the caller commits.

    plans/016 phase 6: `require=False` (Settings → Publish) keeps the run and
    the report but never refuses — the gate entry carries `enforced: False`."""
    from .specs import run_all_specs

    summary = await run_all_specs(session, runner, playbook_id, target, version_n)
    gate = {
        "gate": "specs",
        "ok": summary["failed"] == 0,
        "note": (
            "no specs defined" if summary["total"] == 0
            else f"{summary['passed']}/{summary['total']} passed"
        ),
    }
    if not summary["failed"]:
        return gate, None
    failing = [r for r in summary["results"] if not r["passed"]]
    if not require:
        gate["enforced"] = False
        gate["note"] += " — not enforced (Settings → Publish)"
        return gate, None
    return gate, {
        "error": (
            f"Publish refused — gate 'specs' failed ({len(failing)} of "
            f"{summary['total']} red on version {version_n}). Owner can relax this in Settings → Publish."
        ),
        "message": (
            f"Promote refused — gate 'specs' failed ({len(failing)} of "
            f"{summary['total']} red on v{version_n}). Owner can relax this in Settings → Publish."
        ),
        "gate": "specs",
        "failing_specs": failing,
        "hint": (
            "Fix the candidate via playbook_edit, or update the spec if the "
            "expectation itself changed (playbook_spec_add upserts by name). "
            "Owner can relax this in Settings → Publish."
        ),
    }
