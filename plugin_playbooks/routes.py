"""REST API for the Playbooks plugin.

Mounted at /api/p/plugin-playbooks/
Note: no `from __future__ import annotations` — same Pydantic body fix.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from luna_sdk import get_current_user

from .definition import PlaybookDef, parse_yaml
from .models import (
    Playbook,
    PlaybookDraft,
    PlaybookProbeResult,
    PlaybookRun,
    PlaybookSpec,
    PlaybookStepRun,
    PlaybookVersion,
)
from .probes import run_preflight
from .specs import run_all_specs
from .validation import validate_definition

# 009.001/phase03: every endpoint requires an authenticated user (router-level
# dependency) — these routes mutate playbooks and start agent runs.
router = APIRouter(
    prefix="/api/p/plugin-playbooks",
    tags=["playbooks"],
    dependencies=[Depends(get_current_user)],
)

_session_factory: async_sessionmaker[AsyncSession] | None = None
_runner: Any = None
_events: Any = None
_sync_bindings: Any = None


def init_routes(
    sf: async_sessionmaker[AsyncSession],
    runner: Any,
    events: Any = None,
    sync_bindings: Any = None,
) -> None:
    global _session_factory, _runner, _events, _sync_bindings
    _session_factory = sf
    _runner = runner
    _events = events
    _sync_bindings = sync_bindings


async def _notify_changed(name: str) -> None:
    """Fan out playbook.saved so trigger subscriptions/bindings resync (006.713)."""
    if _events is not None:
        await _events.emit("playbook.saved", {"name": name})


# ---- Pane UI (iframe) ----
# Separate, UNAUTHED router: the browser loads the iframe src with a plain
# GET (no Authorization header). The app inside authenticates every API call
# with the token the Shell posts in (luna-auth). Static assets only.
ui_router = APIRouter(prefix="/api/p/plugin-playbooks", tags=["playbooks-ui"])

_UI_DIR = Path(__file__).parent / "ui"

# Baked into the release; "no-cache" forces an ETag revalidation so a changed
# file is picked up immediately after an upgrade (same policy as marketplace).
_NO_CACHE = {"Cache-Control": "no-cache"}


def _versioned_index() -> Response:
    """index.html with a version query on its asset refs.

    Edge caches (Cloudflare) hold static .js/.css for hours and ignore origin
    no-cache; index.html itself is never edge-cached. Stamping the version onto
    the hashed asset URLs guarantees a fresh fetch on every release.
    """
    # Buster = the PLUGIN's own version: this dist changes exactly when the
    # plugin version does (core releases don't touch it). Also keeps the
    # package free of `luna.*` imports — SDK-only is the published contract.
    try:
        import tomllib

        _v = str(
            tomllib.loads(
                (Path(__file__).parent / "luna-plugin.toml").read_text()
            )["version"]
        )
    except Exception:  # noqa: BLE001 — manifest missing in odd dev layouts
        _v = "0"

    html = (_UI_DIR / "index.html").read_text()
    html = html.replace('.js"', f'.js?v={_v}"').replace('.css"', f'.css?v={_v}"')
    return Response(content=html, media_type="text/html", headers=_NO_CACHE)


@ui_router.get("/ui/")
async def serve_ui_root():
    if (_UI_DIR / "index.html").exists():
        return _versioned_index()
    return Response(
        content="<h1>plugin-playbooks UI not built</h1>", media_type="text/html"
    )


@ui_router.get("/ui/{path:path}")
async def serve_ui(path: str):
    if not path or path == "/":
        path = "index.html"
    target = (_UI_DIR / path).resolve()
    if not str(target).startswith(str(_UI_DIR.resolve())):
        raise HTTPException(403, "Forbidden")
    if not target.exists():
        if (_UI_DIR / "index.html").exists():
            return _versioned_index()
        raise HTTPException(404, "Not found")
    return FileResponse(str(target), headers=_NO_CACHE)


def register_routes(app: Any, ctx: Any) -> None:
    app.include_router(router)
    app.include_router(ui_router)

    # 006.713: reconcile trigger bindings once uvicorn's real event loop runs
    # (plugin on_load happens in a throwaway bootstrap loop). The callback is
    # our own plugin method, handed over via init_routes — no registry walk.
    async def _sync_bindings_on_startup() -> None:
        import asyncio

        async def _go() -> None:
            await asyncio.sleep(3)  # let other plugins finish their startup hooks
            if _sync_bindings is not None:
                try:
                    await _sync_bindings()
                except Exception:  # noqa: BLE001
                    pass

        asyncio.create_task(_go())

    # 0.12.0 (plans/002 phase 5): daily re-probe — catch a credential that
    # died BEFORE a trigger fires into it. Scheduled here for the same
    # reason as the binding sync: on_load's bootstrap loop dies under
    # `luna serve`. New failures post a muted "moment" so the agent tells
    # the owner in chat.
    async def _reprobe_on_startup() -> None:
        import asyncio

        from .probes import reprobe_enabled

        async def _loop() -> None:
            await asyncio.sleep(30)  # let boot settle; first sweep is cheap
            while True:
                try:
                    registry = getattr(_runner, "_tools", None)
                    alerts = await reprobe_enabled(_sf(), registry)
                    if alerts and ctx is not None:
                        lines = "\n".join(
                            f"- **{a['playbook']}** → `{a['tool']}` "
                            f"({a['failure_class']}): {a['detail']}"
                            for a in alerts
                        )
                        await ctx.send_muted_message(
                            title="Playbook preflight found broken tools",
                            content=(
                                "The daily connection check found tools that "
                                "stopped working — these playbooks would fail "
                                "the next time their trigger fires:\n" + lines
                            ),
                            channel="moment",
                        )
                except Exception:  # noqa: BLE001 — the sweep must never die
                    logger.exception("playbooks: re-probe sweep failed")
                await asyncio.sleep(24 * 3600)

        asyncio.create_task(_loop())

    # FastAPI ≥0.136 dropped add_event_handler; the Starlette router list remains.
    app.router.on_startup.append(_sync_bindings_on_startup)
    app.router.on_startup.append(_reprobe_on_startup)


def _sf() -> async_sessionmaker[AsyncSession]:
    assert _session_factory is not None, "Routes not initialized"
    return _session_factory


# ---- Run stats for the list (plans/001) ----
# The list shows when each playbook last ran and how often it runs. Both come
# from one grouped, windowed query over playbook_runs — never one query per
# row, never a scan of the whole run history.

logger = logging.getLogger(__name__)

_STATS_WINDOW_DAYS = 30
_STATS_TTL_SECONDS = 20.0
_stats_cache: dict[str, Any] | None = None


def _reset_stats_cache() -> None:
    """Drop the memoised aggregate — used by tests and after a run finishes."""
    global _stats_cache
    _stats_cache = None


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat those as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# 0.10.0 (plans/002 phase 3): candidate/live plumbing (mirrors agent_tools).
# `version` is the monotonic counter; live content sits on the playbook row
# at version `live_version` (0 = "same as version" on pre-0.10 rows); the one
# un-promoted candidate is a playbook_versions row named by `candidate_version`.

def _live_version_of(p: Playbook) -> int:
    return p.live_version or p.version


async def _get_version_row(
    session: AsyncSession, p: Playbook, n: int,
) -> PlaybookVersion | None:
    return (await session.execute(
        select(PlaybookVersion).where(
            PlaybookVersion.playbook_id == p.id,
            PlaybookVersion.version == n,
        )
    )).scalar_one_or_none()


async def _ensure_live_row(session: AsyncSession, p: Playbook) -> PlaybookVersion:
    """Guarantee a version row exists for the current live content."""
    n = _live_version_of(p)
    row = await _get_version_row(session, p, n)
    if row is None:
        row = PlaybookVersion(
            playbook_id=p.id,
            version=n,
            definition=p.definition,
            code=p.code,
            manifest=p.manifest,
            author="system",
            message="live content (recorded on first candidate/promote)",
        )
        session.add(row)
    return row


def _shim_for(p: Playbook, row: PlaybookVersion) -> Playbook:
    """Transient Playbook carrying a version row's content — lets the runner
    dry-run non-live content untouched. NEVER add it to a session."""
    defn = dict(row.definition)
    defn["name"] = p.name
    return Playbook(
        id=p.id,
        name=p.name,
        display_name=p.display_name,
        description=defn.get("description") or p.description,
        when_to_use=defn.get("when_to_use") or p.when_to_use,
        inputs_schema=defn.get("inputs"),
        definition=defn,
        code=row.code,
        manifest=row.manifest,
        version=row.version,
        live_version=row.version,
        status=p.status,
        agent_autonomy=p.agent_autonomy,
        created_by=p.created_by,
    )


def _runs_per_day(runs: int, created_at: datetime | None, now: datetime) -> float:
    """Runs per day over the days the playbook actually existed in the window.

    A playbook created yesterday that ran four times reads 4.0/day, not 0.1/day.
    """
    window_start = now - timedelta(days=_STATS_WINDOW_DAYS)
    created = _aware(created_at) or window_start
    days = (now - max(created, window_start)).total_seconds() / 86400.0
    days = min(max(days, 1.0), float(_STATS_WINDOW_DAYS))
    return round(runs / days, 1)


async def _run_stats(session: AsyncSession, playbooks: list) -> dict[str, dict[str, Any]]:
    """`{playbook_id: {last_run_at, runs_per_day, runs_window}}` for a list page.

    Two queries at most, both grouped and both index-range scans:
      1. every playbook that ran inside the window (one round trip, all rows);
      2. the last run of the playbooks missing from (1), restricted to those
         ids — skipped when every playbook ran recently.
    Results are memoised for `_STATS_TTL_SECONDS`, so repeated mounts of the
    pane cost nothing. Stats trail reality by design; 20s is invisible.
    """
    global _stats_cache
    now = datetime.now(timezone.utc)
    cache = _stats_cache
    if cache is None or (time.monotonic() - cache["at"]) > _STATS_TTL_SECONDS:
        rows = (await session.execute(
            select(
                PlaybookRun.playbook_id,
                func.count(PlaybookRun.id),
                func.max(PlaybookRun.started_at),
            )
            .where(PlaybookRun.started_at >= now - timedelta(days=_STATS_WINDOW_DAYS))
            .group_by(PlaybookRun.playbook_id)
        )).all()
        cache = {
            "at": time.monotonic(),
            "recent": {str(pid): (count, last) for pid, count, last in rows},
            # Playbooks with nothing in the window; resolved lazily, and
            # remembered as None when they have never run at all.
            "idle": {},
        }
        _stats_cache = cache

    recent, idle = cache["recent"], cache["idle"]
    unknown = [
        p.id for p in playbooks
        if str(p.id) not in recent and str(p.id) not in idle
    ]
    if unknown:
        rows = (await session.execute(
            select(PlaybookRun.playbook_id, func.max(PlaybookRun.started_at))
            .where(PlaybookRun.playbook_id.in_(unknown))
            .group_by(PlaybookRun.playbook_id)
        )).all()
        found = {str(pid): last for pid, last in rows}
        for pid in unknown:
            idle[str(pid)] = found.get(str(pid))

    out: dict[str, dict[str, Any]] = {}
    for p in playbooks:
        key = str(p.id)
        if key in recent:
            count, last = recent[key]
        else:
            count, last = 0, idle.get(key)
        last = _aware(last)
        out[key] = {
            "last_run_at": last.isoformat() if last else None,
            "runs_window": count,
            "runs_per_day": _runs_per_day(count, getattr(p, "created_at", None), now),
        }
    return out


class PlaybookCreate(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    when_to_use: str = ""
    definition_yaml: str
    agent_autonomy: str = "agent_must_confirm"


class PlaybookUpdate(BaseModel):
    definition_yaml: str
    message: str = ""


class AutonomyPatch(BaseModel):
    agent_autonomy: str


class RunCreate(BaseModel):
    inputs: dict[str, Any] = {}
    trigger: str = "api"


async def _trust_summaries(session, playbook_ids):
    """Phase 6: per-playbook trust data for the list badges — two batched
    GROUP BY queries (specs + probes), never N+1."""
    trust: dict[str, dict[str, Any]] = {
        str(pid): {
            "specs": {"total": 0, "failed": 0, "last_run_at": None},
            "probes": {"total": 0, "failed": 0, "probed_at": None},
        }
        for pid in playbook_ids
    }
    if not playbook_ids:
        return trust
    spec_rows = (await session.execute(
        select(
            PlaybookSpec.playbook_id,
            func.count(PlaybookSpec.id),
            func.max(PlaybookSpec.last_run_at),
        )
        .where(PlaybookSpec.playbook_id.in_(playbook_ids))
        .group_by(PlaybookSpec.playbook_id)
    )).all()
    # pass/fail needs the JSON last_result — fetch the failing count separately
    # per DB-portable path: pull (playbook_id, last_result) and count in Python
    # (spec counts are tiny).
    spec_results = (await session.execute(
        select(PlaybookSpec.playbook_id, PlaybookSpec.last_result)
        .where(PlaybookSpec.playbook_id.in_(playbook_ids))
    )).all()
    failed_by_pb: dict[str, int] = {}
    for pid, last in spec_results:
        if isinstance(last, dict) and last.get("passed") is False:
            failed_by_pb[str(pid)] = failed_by_pb.get(str(pid), 0) + 1
    for pid, total, last_run in spec_rows:
        trust[str(pid)]["specs"] = {
            "total": total,
            "failed": failed_by_pb.get(str(pid), 0),
            "last_run_at": last_run.isoformat() if last_run else None,
        }
    probe_rows = (await session.execute(
        select(
            PlaybookProbeResult.playbook_id,
            func.count(PlaybookProbeResult.id),
            func.max(PlaybookProbeResult.probed_at),
        )
        .where(PlaybookProbeResult.playbook_id.in_(playbook_ids))
        .group_by(PlaybookProbeResult.playbook_id)
    )).all()
    probe_failed = (await session.execute(
        select(PlaybookProbeResult.playbook_id, func.count(PlaybookProbeResult.id))
        .where(
            PlaybookProbeResult.playbook_id.in_(playbook_ids),
            PlaybookProbeResult.status == "failed",
        )
        .group_by(PlaybookProbeResult.playbook_id)
    )).all()
    failed_probes = {str(pid): n for pid, n in probe_failed}
    for pid, total, probed_at in probe_rows:
        trust[str(pid)]["probes"] = {
            "total": total,
            "failed": failed_probes.get(str(pid), 0),
            "probed_at": probed_at.isoformat() if probed_at else None,
        }
    return trust


@router.get("/playbooks")
async def list_playbooks(status: str = "active"):
    async with _sf()() as session:
        stmt = select(Playbook)
        if status == "active":
            stmt = stmt.where(Playbook.status.in_(["enabled", "disabled"]))
        elif status != "all":
            stmt = stmt.where(Playbook.status == status)
        rows = (await session.execute(stmt)).scalars().all()
        try:
            stats = await _run_stats(session, list(rows))
        except Exception as e:  # pragma: no cover - depends on the DB
            # Run history is a nice-to-have on this screen; never let it stop
            # the list from rendering.
            logger.warning("playbooks: run stats unavailable: %s", e)
            stats = {}
        try:
            trust = await _trust_summaries(session, [p.id for p in rows])
        except Exception as e:  # pragma: no cover - same posture as stats
            logger.warning("playbooks: trust summaries unavailable: %s", e)
            trust = {}
        return [{
            "trust": {
                **trust.get(str(p.id), {
                    "specs": {"total": 0, "failed": 0, "last_run_at": None},
                    "probes": {"total": 0, "failed": 0, "probed_at": None},
                }),
                "manifest_present": bool((p.manifest or "").strip()),
            },
            "id": str(p.id),
            "name": p.name,
            "display_name": p.display_name,
            "description": p.description,
            "when_to_use": p.when_to_use,
            "status": p.status,
            "agent_autonomy": p.agent_autonomy,
            "version": p.version,
            "live_version": _live_version_of(p),
            "candidate_version": p.candidate_version,
            "cost_estimate_cents": p.cost_estimate_cents,
            "duration_estimate_ms": p.duration_estimate_ms,
            **stats.get(str(p.id), {
                "last_run_at": None, "runs_window": 0, "runs_per_day": 0.0,
            }),
        } for p in rows]


@router.get("/playbooks/{name}")
async def get_playbook(name: str):
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")
        candidate_definition = None
        candidate_code = None
        if p.candidate_version:
            cand = await _get_version_row(session, p, p.candidate_version)
            if cand is not None:
                candidate_definition = cand.definition
                candidate_code = cand.code
        return {
            "id": str(p.id),
            "name": p.name,
            "display_name": p.display_name,
            "description": p.description,
            "when_to_use": p.when_to_use,
            "definition": p.definition,
            "code": p.code,
            "manifest": p.manifest,
            "inputs_schema": p.inputs_schema,
            "status": p.status,
            "agent_autonomy": p.agent_autonomy,
            "version": p.version,
            "live_version": _live_version_of(p),
            "candidate_version": p.candidate_version,
            "candidate_definition": candidate_definition,
            "candidate_code": candidate_code,
        }


@router.post("/playbooks")
async def create_playbook(body: PlaybookCreate):
    try:
        pb_def = parse_yaml(body.definition_yaml)
    except Exception as e:
        raise HTTPException(400, f"Invalid YAML: {e}")

    import yaml as _yaml
    tool_registry = getattr(_runner, "_tools", None)
    issues = validate_definition(
        _yaml.safe_load(body.definition_yaml),
        tool_registry=tool_registry, check_unknown_keys=True,
    )
    errors = [i.to_dict() for i in issues if i.severity == "error"]
    if errors:
        raise HTTPException(422, {"message": "Playbook is invalid", "issues": errors})

    async with _sf()() as session:
        existing = (await session.execute(
            select(Playbook).where(Playbook.name == body.name)
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(409, f"Playbook '{body.name}' already exists")

        try:
            from .pblang import generate_code
            _code = generate_code(pb_def)
        except Exception:  # noqa: BLE001
            _code = None
        p = Playbook(
            name=body.name,
            display_name=body.display_name or pb_def.display_name or body.name,
            description=body.description or pb_def.description,
            when_to_use=body.when_to_use or pb_def.when_to_use,
            inputs_schema=pb_def.inputs,
            definition=pb_def.model_dump(mode="json", exclude_none=True, by_alias=True),
            code=_code,
            live_version=1,  # a brand-new playbook goes live directly
            agent_autonomy=body.agent_autonomy,
            created_by="owner",
            status="enabled",
        )
        session.add(p)
        await session.commit()
        await session.refresh(p)
    await _notify_changed(body.name)
    return {"id": str(p.id), "name": p.name, "status": "created"}


@router.put("/playbooks/{name}")
async def update_playbook(name: str, body: PlaybookUpdate):
    try:
        pb_def = parse_yaml(body.definition_yaml)
    except Exception as e:
        raise HTTPException(400, f"Invalid YAML: {e}")

    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")

        # 0.10.0: the owner edits LIVE directly (the UI is the owner) — record
        # the old live content, then create a row for the new live version.
        # Never snapshot at the counter: a pending candidate may own that
        # number, and version numbers must stay unique.
        await _ensure_live_row(session, p)
        p.definition = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
        # 0.8.0: the YAML write invalidates any stored pblang code — regenerate
        # (or NULL it so reads derive fresh; stale code must never survive).
        try:
            from .pblang import generate_code
            p.code = generate_code(pb_def)
        except Exception:  # noqa: BLE001
            p.code = None
        p.version += 1
        session.add(PlaybookVersion(
            playbook_id=p.id,
            version=p.version,
            definition=p.definition,
            code=p.code,
            manifest=p.manifest,
            author="owner",
            message=body.message or "REST update",
        ))
        p.live_version = p.version
        p.description = pb_def.description or p.description
        p.when_to_use = pb_def.when_to_use or p.when_to_use
        p.display_name = pb_def.display_name or p.display_name
        p.inputs_schema = pb_def.inputs
        await session.commit()
        version = p.version
    await _notify_changed(name)
    return {"name": name, "version": version, "status": "updated"}


@router.post("/playbooks/{name}/enable")
async def enable_playbook(name: str):
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404)
        p.status = "enabled"
        await session.commit()
    await _notify_changed(name)
    return {"name": name, "status": "enabled"}


@router.post("/playbooks/{name}/disable")
async def disable_playbook(name: str):
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404)
        p.status = "disabled"
        await session.commit()
    await _notify_changed(name)
    return {"name": name, "status": "disabled"}


class PlaybookPatch(BaseModel):
    enabled: Optional[bool] = None
    display_name: Optional[str] = None
    description: Optional[str] = None


@router.patch("/playbooks/{name}")
async def patch_playbook(name: str, body: PlaybookPatch):
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404)
        if body.enabled is not None:
            p.status = "enabled" if body.enabled else "disabled"
        if body.display_name is not None:
            p.display_name = body.display_name
        if body.description is not None:
            p.description = body.description
        await session.commit()
        status_out = p.status
    await _notify_changed(name)
    return {"name": name, "status": status_out}


@router.patch("/playbooks/{name}/autonomy")
async def patch_autonomy(name: str, body: AutonomyPatch):
    valid = {"manual_only", "agent_may_trigger", "agent_must_confirm"}
    if body.agent_autonomy not in valid:
        raise HTTPException(400, f"Invalid autonomy: {body.agent_autonomy}")

    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404)
        p.agent_autonomy = body.agent_autonomy
        await session.commit()
        return {"name": name, "agent_autonomy": body.agent_autonomy}


@router.delete("/playbooks/{name}")
async def archive_playbook(name: str):
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404)
        p.status = "archived"
        await session.commit()
    await _notify_changed(name)
    return {"name": name, "status": "archived"}


@router.post("/playbooks/{name}/runs")
async def start_run(name: str, body: RunCreate):
    if not _runner:
        raise HTTPException(503, "Runner not initialized")

    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")

    # plans/009: background start — the Run button responds instantly instead
    # of hanging for the whole run; liveness comes from activity.* heartbeats.
    run = await _runner.start_run_background(p, inputs=body.inputs, trigger=body.trigger)
    _reset_stats_cache()  # a run the owner just started should show as "now"
    return {"run_id": str(run.id), "status": run.status}


@router.get("/playbooks/{name}/runs")
async def list_runs(name: str):
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404)

        runs = (await session.execute(
            select(PlaybookRun).where(PlaybookRun.playbook_id == p.id).order_by(PlaybookRun.started_at.desc())
        )).scalars().all()

        return [{
            "id": str(r.id),
            "status": r.status,
            "trigger": r.trigger,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        } for r in runs]


@router.get("/playbooks/runs/{run_id}")
async def get_run(run_id: str):
    async with _sf()() as session:
        run = await session.get(PlaybookRun, uuid.UUID(run_id))
        if not run:
            raise HTTPException(404)

        steps = (await session.execute(
            select(PlaybookStepRun)
            .where(PlaybookStepRun.run_id == run.id)
            .order_by(PlaybookStepRun.started_at)
        )).scalars().all()

        return {
            "id": str(run.id),
            "status": run.status,
            "trigger": run.trigger,
            "inputs": run.inputs,
            "steps": [{
                "step_id": s.step_id,
                "kind": s.step_kind,
                "status": s.status,
                "inputs": s.inputs,
                "outputs": s.outputs,
                "error": s.error,
                "retry_count": s.retry_count,
                "cost_cents": s.cost_cents,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            } for s in steps],
        }


@router.post("/playbooks/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    if not _runner:
        raise HTTPException(503)
    await _runner.cancel_run(uuid.UUID(run_id))
    return {"run_id": run_id, "status": "cancelled"}


# --- Versions ---


@router.get("/playbooks/{name}/versions")
async def list_versions(name: str):
    """List version history for a playbook, newest first, with run counts."""
    from sqlalchemy import func as sa_func

    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")

        versions = (await session.execute(
            select(PlaybookVersion)
            .where(PlaybookVersion.playbook_id == p.id)
            .order_by(PlaybookVersion.version.desc())
        )).scalars().all()

        run_counts: dict[int, int] = {}
        rows = (await session.execute(
            select(PlaybookRun.playbook_version, sa_func.count())
            .where(PlaybookRun.playbook_id == p.id)
            .group_by(PlaybookRun.playbook_version)
        )).all()
        for ver_num, cnt in rows:
            run_counts[ver_num] = cnt

        # 0.10.0: rows are the history; live/candidate are pointers into it.
        # Legacy playbooks may lack a row for the current live version — the
        # synthesized entry covers that gap only (no duplicates).
        live_n = _live_version_of(p)
        result = []
        if not any(v.version == live_n for v in versions):
            result.append({
                "version": live_n,
                "title": "",
                "author": "",
                "created_at": p.updated_at.isoformat(),
                "runs": run_counts.get(live_n, 0),
                "promoted_from": None,
                "current": True,
                "live": True,
                "candidate": False,
            })

        for v in versions:
            result.append({
                "version": v.version,
                "title": v.message,
                "author": v.author,
                "created_at": v.created_at.isoformat(),
                "runs": run_counts.get(v.version, 0),
                "promoted_from": v.promoted_from,
                "current": v.version == live_n,
                "live": v.version == live_n,
                "candidate": v.version == p.candidate_version,
            })

        result.sort(key=lambda r: r["version"], reverse=True)
        return result


class PromoteBody(BaseModel):
    # None → promote the CANDIDATE through the gate; a number → owner-restore
    # that stored version to live (pointer move, no gate beyond existence).
    version: int | None = None


def _apply_row_to_live(p: Playbook, row: PlaybookVersion, *, restore_manifest: bool) -> None:
    """Make a version row's content the live content (pointer + fields)."""
    defn = dict(row.definition)
    defn["name"] = p.name
    p.definition = defn
    p.code = row.code
    if restore_manifest:
        p.manifest = row.manifest
    p.description = defn.get("description") or p.description
    p.when_to_use = defn.get("when_to_use") or p.when_to_use
    p.display_name = defn.get("display_name") or p.display_name
    p.inputs_schema = defn.get("inputs")
    p.live_version = row.version


@router.post("/playbooks/{name}/promote")
async def promote_version(name: str, body: PromoteBody):
    """0.10.0: pointer semantics — no new version numbers are minted.

    Without a version: promote the pending CANDIDATE through the gate
    (static validation; drift was enforced when the candidate was saved).
    With a version: owner-restore that stored version to live.
    """
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")

        target_n = body.version
        candidate = target_n is None
        if candidate:
            if not p.candidate_version:
                raise HTTPException(409, f"'{name}' has no candidate to promote")
            target_n = p.candidate_version

        row = await _get_version_row(session, p, target_n)
        if not row:
            raise HTTPException(404, f"Version {target_n} not found")

        if candidate:
            # THE GATE (extensible — the probes gate plugs in here, phase 5).
            issues = validate_definition(
                row.definition,
                tool_registry=getattr(_runner, "_tools", None),
                check_unknown_keys=False,
            )
            errors = [i.to_dict() for i in issues if i.severity == "error"]
            if errors:
                raise HTTPException(422, {
                    "message": "Promote refused — gate 'static_validation' failed",
                    "gate": "static_validation",
                    "issues": errors,
                })
            # 0.11.0: specs gate — every stored spec must pass against the
            # candidate (same gate as the playbook_promote tool).
            spec_summary = await run_all_specs(
                session, _runner, p.id, _shim_for(p, row), row.version,
            )
            if spec_summary["failed"]:
                await session.commit()  # persist last_result on spec rows
                raise HTTPException(422, {
                    "message": "Promote refused — gate 'specs' failed",
                    "gate": "specs",
                    "failing_specs": [
                        r for r in spec_summary["results"] if not r["passed"]
                    ],
                })
            # 0.12.0: probes gate — no tool the candidate touches may be
            # KNOWN-broken (unprobeable tools pass; see probes.py).
            probe_summary = await run_preflight(
                session, getattr(_runner, "_tools", None), p,
                row.definition or {},
            )
            if probe_summary["failed"]:
                await session.commit()  # persist the probe cache rows
                raise HTTPException(422, {
                    "message": "Promote refused — gate 'probes' failed",
                    "gate": "probes",
                    "failing_tools": [
                        r for r in probe_summary["results"]
                        if r["status"] == "failed"
                    ],
                })

        old_live = _live_version_of(p)
        await _ensure_live_row(session, p)
        # candidate promote keeps the live manifest (manifest is live-owned);
        # an owner restore of an old version brings its manifest back too.
        _apply_row_to_live(p, row, restore_manifest=not candidate)
        row.promoted_from = old_live  # rollback lineage
        if p.candidate_version == target_n:
            p.candidate_version = None

        await session.commit()
        result = {
            "name": name,
            "live_version": target_n,
            "version": p.version,
            "promoted_from": old_live,
            "status": "promoted",
        }
    await _notify_changed(name)
    return result


@router.post("/playbooks/{name}/rollback")
async def rollback_playbook(name: str):
    """Restore the previous live version (the one live was promoted from)."""
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")

        live_n = _live_version_of(p)
        live_row = await _get_version_row(session, p, live_n)
        target_n = live_row.promoted_from if live_row else None
        if not target_n:
            from sqlalchemy import func as sa_func
            target_n = (await session.execute(
                select(sa_func.max(PlaybookVersion.version)).where(
                    PlaybookVersion.playbook_id == p.id,
                    PlaybookVersion.version < live_n,
                )
            )).scalar()
        if not target_n:
            raise HTTPException(409, f"'{name}' has no previous version to roll back to")
        row = await _get_version_row(session, p, target_n)
        if not row:
            raise HTTPException(404, f"No stored content for version {target_n}")

        await _ensure_live_row(session, p)
        _apply_row_to_live(p, row, restore_manifest=True)
        await session.commit()
        result = {
            "name": name,
            "live_version": target_n,
            "rolled_back_from": live_n,
            "status": "rolled_back",
        }
    await _notify_changed(name)
    return result


# --- Specs (0.11.0, plans/002 phase 4) ---

@router.get("/playbooks/{name}/specs")
async def list_specs(name: str):
    """The playbook's tests + last results — feeds the phase-6 Tests tab."""
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")
        rows = (await session.execute(
            select(PlaybookSpec)
            .where(PlaybookSpec.playbook_id == p.id)
            .order_by(PlaybookSpec.name)
        )).scalars().all()
        return {
            "name": name,
            "specs": [
                {
                    "name": r.name,
                    "spec": r.spec,
                    "created_by": r.created_by,
                    "last_result": r.last_result,
                    "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                    "last_version": r.last_version,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ],
        }


@router.post("/playbooks/{name}/specs/run")
async def run_specs_route(name: str):
    """Run all specs against the candidate (or live when none) — the Tests
    tab's run-all button."""
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")
        if p.candidate_version:
            row = await _get_version_row(session, p, p.candidate_version)
            if not row:
                raise HTTPException(409, "Candidate version row is missing")
            target, version_n = _shim_for(p, row), row.version
        else:
            target, version_n = p, _live_version_of(p)
        summary = await run_all_specs(
            session, _runner, p.id, target, version_n,
        )
        await session.commit()
        return {"name": name, "ran_against_version": version_n, **summary}


# --- Probes (0.12.0, plans/002 phase 5) ---

@router.get("/playbooks/{name}/probes")
async def list_probes(name: str):
    """Cached preflight results per tool — feeds the phase-6 trust badges."""
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")
        rows = (await session.execute(
            select(PlaybookProbeResult)
            .where(PlaybookProbeResult.playbook_id == p.id)
            .order_by(PlaybookProbeResult.tool)
        )).scalars().all()
        return {
            "name": name,
            "probes": [
                {
                    "tool": r.tool,
                    "status": r.status,
                    "failure_class": r.failure_class,
                    "detail": r.detail,
                    "probed_at": r.probed_at.isoformat() if r.probed_at else None,
                }
                for r in rows
            ],
        }


@router.post("/playbooks/{name}/preflight")
async def run_preflight_route(name: str):
    """Probe every tool the playbook touches now (candidate when one
    exists, else live) — the Tests tab's connections check."""
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")
        if p.candidate_version:
            row = await _get_version_row(session, p, p.candidate_version)
            if not row:
                raise HTTPException(409, "Candidate version row is missing")
            definition, version_n = row.definition or {}, row.version
        else:
            definition, version_n = p.definition or {}, _live_version_of(p)
        summary = await run_preflight(
            session, getattr(_runner, "_tools", None), p, definition,
        )
        await session.commit()
        return {"name": name, "checked_version": version_n, **summary}


# --- Manifest (0.9.0, plans/002 phase 2) ---

class ManifestBody(BaseModel):
    manifest: str


@router.get("/playbooks/{name}/manifest")
async def get_manifest(name: str):
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")
        return {"name": name, "manifest": p.manifest}


@router.put("/playbooks/{name}/manifest")
async def put_manifest(name: str, body: ManifestBody):
    """Owner sets the manifest directly — no approval gate on the REST path
    (the UI IS the owner); the agent path goes through playbook_manifest_set,
    which raises an approval card."""
    async with _sf()() as session:
        p = (await session.execute(
            select(Playbook).where(Playbook.name == name)
        )).scalar_one_or_none()
        if not p:
            raise HTTPException(404, f"Playbook '{name}' not found")
        # 0.10.0: manifest is LIVE content — record old live, then create the
        # new live version row (never snapshot at the counter: a pending
        # candidate may own that number).
        await _ensure_live_row(session, p)
        p.manifest = body.manifest
        p.version += 1
        session.add(PlaybookVersion(
            playbook_id=p.id,
            version=p.version,
            definition=p.definition,
            code=p.code,
            manifest=p.manifest,
            author="owner",
            message="manifest updated",
        ))
        p.live_version = p.version
        await session.commit()
        version = p.version
    await _notify_changed(name)
    return {"name": name, "version": version, "status": "manifest_set"}


# --- Drafts ---

@router.get("/drafts")
async def list_drafts():
    async with _sf()() as session:
        rows = (await session.execute(select(PlaybookDraft))).scalars().all()
        return [{
            "id": str(d.id),
            "name": d.name,
            "playbook_id": str(d.playbook_id) if d.playbook_id else None,
            "updated_at": d.updated_at.isoformat(),
        } for d in rows]


class DraftCreate(BaseModel):
    name: str = ""
    display_name: str = ""


@router.post("/drafts")
async def create_draft(body: DraftCreate | None = None):
    """Create a blank playbook draft for the canvas editor."""
    async with _sf()() as session:
        existing = (await session.execute(select(PlaybookDraft))).scalars().all()
        seq = len(existing) + 1
        name = (body and body.name) or f"untitled-{seq}"
        display_name = (body and body.display_name) or name.replace("-", " ").title()
        blank_def = {
            "name": name,
            "display_name": display_name,
            "description": "",
            "when_to_use": "",
            "agent_autonomy": "agent_must_confirm",
            "triggers": [],
            "steps": [],
        }
        draft = PlaybookDraft(
            name=name,
            definition=blank_def,
            created_by="owner",
        )
        session.add(draft)
        await session.commit()
        await session.refresh(draft)
        return {
            "id": str(draft.id),
            "name": draft.name,
            "definition": draft.definition,
        }


@router.get("/drafts/{draft_id}")
async def get_draft(draft_id: str):
    async with _sf()() as session:
        d = await session.get(PlaybookDraft, uuid.UUID(draft_id))
        if not d:
            raise HTTPException(404)
        return {
            "id": str(d.id),
            "name": d.name,
            "definition": d.definition,
        }


@router.put("/drafts/{draft_id}")
async def update_draft(draft_id: str, body: dict):
    """Save definition changes back to a draft."""
    async with _sf()() as session:
        d = await session.get(PlaybookDraft, uuid.UUID(draft_id))
        if not d:
            raise HTTPException(404)
        if "definition" in body:
            d.definition = body["definition"]
        if "name" in body:
            d.name = body["name"]
        await session.commit()
        return {"id": str(d.id), "name": d.name}


@router.post("/drafts/{draft_id}/promote")
async def promote_draft(draft_id: str):
    """Promote a draft to a live playbook."""
    async with _sf()() as session:
        d = await session.get(PlaybookDraft, uuid.UUID(draft_id))
        if not d:
            raise HTTPException(404)
        defn = d.definition or {}

        existing = (await session.execute(
            select(Playbook).where(Playbook.name == d.name)
        )).scalar_one_or_none()
        if existing:
            raise HTTPException(409, f"Playbook '{d.name}' already exists")

        p = Playbook(
            name=d.name,
            display_name=defn.get("display_name", d.name),
            description=defn.get("description", ""),
            when_to_use=defn.get("when_to_use", ""),
            inputs_schema=defn.get("inputs", {}),
            definition=defn,
            live_version=1,  # a brand-new playbook goes live directly
            agent_autonomy=defn.get("agent_autonomy", "manual_only"),
            created_by="owner",
            status="enabled",
        )
        session.add(p)
        await session.delete(d)
        await session.commit()
        await session.refresh(p)
        return {"id": str(p.id), "name": p.name, "status": "created"}


@router.delete("/drafts/{draft_id}")
async def delete_draft(draft_id: str):
    async with _sf()() as session:
        d = await session.get(PlaybookDraft, uuid.UUID(draft_id))
        if not d:
            raise HTTPException(404)
        await session.delete(d)
        await session.commit()
        return {"id": draft_id, "status": "deleted"}
