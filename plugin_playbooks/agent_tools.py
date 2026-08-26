"""Agent tools for the Playbooks plugin.

These are the tools Luna uses to propose, list, run, and manage playbooks.

006.714: authoring is whole-YAML only. `playbook_propose` creates from full
YAML; `playbook_edit` rewrites an existing playbook from full YAML (snapshot →
validate → replace). The granular node tools (add/update/remove step, new
version, create draft, add trigger, save) were removed — they led the agent to
build playbooks piecemeal. To change a playbook: `playbook_get_definition` →
edit the whole YAML → `playbook_edit`; `playbook_validate` / `playbook_dry_run`
to check before `playbook_run`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from luna_sdk import EventBus, ToolDef

from .definition import AgentAutonomy, PlaybookDef, parse_yaml
from .models import (
    Playbook,
    PlaybookEditTicket,
    PlaybookRun,
    PlaybookStepRun,
    PlaybookVersion,
)
from .pblang import PlaybookCompileError, compile_playbook, generate_code
from .validation import validate_definition


def _compile_code(code: str, *, name: str) -> tuple[PlaybookDef | None, str | None]:
    """(def, None) on success, (None, json error payload) on compile errors."""
    try:
        return compile_playbook(code, name=name), None
    except PlaybookCompileError as e:
        return None, json.dumps({
            "error": "The playbook code does not compile — fix these and retry.",
            "issues": [i.to_dict() for i in e.issues],
        })


def _derive_code(playbook: Playbook) -> str:
    """The playbook's pblang source — stored, or derived via codegen."""
    if playbook.code:
        return playbook.code
    return generate_code(PlaybookDef.model_validate(playbook.definition))


def _codegen_or_none(pb_def: PlaybookDef) -> str | None:
    try:
        return generate_code(pb_def)
    except Exception:  # noqa: BLE001 — code is derivable on read; never block
        return None


async def _load_all_playbook_steps(
    session: AsyncSession, exclude: str | None = None,
) -> dict[str, Any]:
    """{name: [StepDef,...]} for every saved playbook — feeds subtask-cycle
    detection in the validator."""
    rows = (await session.execute(select(Playbook))).scalars().all()
    out: dict[str, Any] = {}
    for r in rows:
        if exclude and r.name == exclude:
            continue
        try:
            out[r.name] = PlaybookDef.model_validate(r.definition).steps
        except Exception:  # noqa: BLE001
            continue
    return out


# 0.9.0 (plans/002 phase 2): staged-edit tickets — single-use, 15-minute TTL.
_TICKET_TTL_SECONDS = 15 * 60


def _aware(dt):
    """SQLite round-trips tz-aware datetimes as naive UTC — normalize."""
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _issue_ticket(session: AsyncSession, playbook: Playbook) -> PlaybookEditTicket:
    """Create a fresh edit ticket; convergently sweep dead ones while here."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete, or_

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_TICKET_TTL_SECONDS)
    await session.execute(delete(PlaybookEditTicket).where(or_(
        PlaybookEditTicket.created_at < cutoff,
        PlaybookEditTicket.used_at.is_not(None),
    )))
    ticket = PlaybookEditTicket(
        playbook_id=playbook.id, base_version=playbook.version,
    )
    session.add(ticket)
    await session.flush()
    return ticket


_TICKET_HINT = (
    "Call playbook_edit(name) with NO other arguments first — it returns the "
    "manifest, the current code, and a fresh edit ticket."
)


async def _check_ticket(
    session: AsyncSession, playbook: Playbook, ticket: str, *, consume: bool,
) -> str | None:
    """None when the ticket is valid, else a refusal message.

    consume=True marks it used (call only at the point of a successful save —
    a compile error or drift refusal must NOT burn the ticket).
    """
    from datetime import datetime, timedelta, timezone

    if not ticket:
        return "An edit ticket is required to save changes. " + _TICKET_HINT
    try:
        tid = uuid.UUID(ticket)
    except ValueError:
        return "Invalid edit ticket. " + _TICKET_HINT
    row = (await session.execute(
        select(PlaybookEditTicket).where(PlaybookEditTicket.id == tid)
    )).scalar_one_or_none()
    if row is None or row.playbook_id != playbook.id:
        return "Unknown edit ticket for this playbook. " + _TICKET_HINT
    if row.used_at is not None:
        return "This edit ticket was already used. " + _TICKET_HINT
    age = datetime.now(timezone.utc) - _aware(row.created_at)
    if age > timedelta(seconds=_TICKET_TTL_SECONDS):
        return "This edit ticket expired. " + _TICKET_HINT
    if row.base_version != playbook.version:
        return (
            "The playbook changed while you were editing (your ticket was "
            "issued for an older version). " + _TICKET_HINT
        )
    if consume:
        row.used_at = datetime.now(timezone.utc)
    return None


def build_tools(
    session_factory: async_sessionmaker[AsyncSession],
    events: EventBus,
    runner: Any,
) -> list[tuple[ToolDef, Any]]:
    """Return (ToolDef, handler) pairs for all playbook agent tools."""

    tools: list[tuple[ToolDef, Any]] = []

    # --- playbook_propose ---
    async def _propose(
        *,
        name: str,
        display_name: str = "",
        description: str = "",
        when_to_use: str = "",
        code: str = "",
        definition_yaml: str = "",
        manifest: str = "",
        agent_autonomy: str = "agent_must_confirm",
    ) -> str:
        # 0.8.0 (plans/002 phase 1): code is the preferred authoring format;
        # definition_yaml stays accepted until the migration phase removes it.
        if bool(code) == bool(definition_yaml):
            return json.dumps({
                "error": "Provide exactly one of 'code' (preferred) or "
                         "'definition_yaml'.",
            })
        if code:
            pb_def, err = _compile_code(code, name=name)
            if err:
                return err
            stored_code: str | None = code
        else:
            try:
                pb_def = parse_yaml(definition_yaml)
            except Exception as e:
                return json.dumps({"error": f"Invalid YAML: {e}"})
            stored_code = _codegen_or_none(pb_def)

        defn = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
        defn["name"] = name

        async with session_factory() as session:
            existing = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if existing:
                return json.dumps({"error": f"Playbook '{name}' already exists"})
            all_pb = await _load_all_playbook_steps(session, exclude=name)

            # YAML path: validate the raw mapping so unknown keys (typos) are
            # caught — the pydantic dump silently drops them. Code path: the
            # compiler already rejects unknown kwargs, and the dump carries
            # cross-kind defaults (fan_in/concurrency/...) the key checker
            # would falsely flag — so skip the unknown-key check there.
            if definition_yaml:
                import yaml as _yaml
                check_target = _yaml.safe_load(definition_yaml)
            else:
                check_target = defn
            issues = validate_definition(
                check_target,
                tool_registry=getattr(runner, "_tools", None), all_playbooks=all_pb,
                check_unknown_keys=bool(definition_yaml),
            )
            errors = [i.to_dict() for i in issues if i.severity == "error"]
            if errors:
                return json.dumps({
                    "error": "Playbook is invalid — fix these before it can be created.",
                    "issues": errors,
                })
            warnings = [i.to_dict() for i in issues if i.severity == "warning"]

            playbook = Playbook(
                name=name,
                display_name=display_name or pb_def.display_name or name,
                description=description or pb_def.description,
                when_to_use=when_to_use or pb_def.when_to_use,
                inputs_schema=pb_def.inputs,
                definition=defn,
                code=stored_code,
                manifest=manifest,
                live_version=1,  # a brand-new playbook goes live directly
                agent_autonomy=agent_autonomy,
                created_by="agent",
                status="enabled",
            )
            session.add(playbook)
            await session.commit()
            await session.refresh(playbook)

        await events.emit("playbook.created", {
            "playbook_id": str(playbook.id),
            "name": name,
            "created_by": "agent",
        })
        # 006.714 → 009.001/phase04: open the canvas (by NAME — a live
        # playbook, not a draft) so the owner sees the whole playbook the
        # moment it's created. Rides the generic E12 plugin-event envelope;
        # focus switches the Shell to the playbooks section.
        await events.emit("ui.plugin.event", {
            "plugin": "plugin-playbooks",
            "event": "playbook.open",
            "payload": {"draft_id": name, "name": name},
            "focus": True,
        })
        return json.dumps({
            "playbook_id": str(playbook.id),
            "name": name,
            "status": "created",
            "warnings": warnings,
        })

    tools.append((
        ToolDef(
            name="playbook_propose",
            chat_only=True,
            description=(
                "Create a new playbook from its FULL source, written all at "
                "once. PREFERRED: pass `code` — the playbook language "
                "(restricted Python: playbook(...) header, then "
                "x = tool(...)/llm(...)/loop(...)/if_(...) steps; see the "
                "playbook-authoring skill). The code is parsed and compiled, "
                "never executed. Legacy: `definition_yaml` (full YAML IR). "
                "Pass exactly one of the two."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique kebab-case name"},
                    "display_name": {"type": "string", "description": "Human-friendly name"},
                    "description": {"type": "string"},
                    "when_to_use": {"type": "string"},
                    "code": {
                        "type": "string",
                        "description": "Full playbook code (preferred format)",
                    },
                    "definition_yaml": {
                        "type": "string",
                        "description": "Full YAML definition (legacy format)",
                    },
                    "manifest": {
                        "type": "string",
                        "description": (
                            "Optional intent manifest (plain markdown: "
                            "Purpose, Side effects, Never, Acceptance). "
                            "Future edits are checked against it."
                        ),
                    },
                    "agent_autonomy": {
                        "type": "string",
                        "enum": ["agent_must_confirm", "agent_may_trigger"],
                        "default": "agent_must_confirm",
                    },
                },
                "required": ["name"],
            },
        ),
        _propose,
    ))

    # --- playbook_list ---
    async def _list(*, filter: str = "enabled") -> str:
        async with session_factory() as session:
            stmt = select(Playbook)
            if filter == "enabled":
                stmt = stmt.where(Playbook.status == "enabled")
            rows = (await session.execute(stmt)).scalars().all()
            return json.dumps([{
                "name": p.name,
                "display_name": p.display_name,
                "description": p.description,
                "when_to_use": p.when_to_use,
                "agent_autonomy": p.agent_autonomy,
                "status": p.status,
            } for p in rows])

    tools.append((
        ToolDef(
            name="playbook_list",
            description="List available playbooks.",
            parameters={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "enum": ["all", "enabled", "disabled", "archived"],
                        "default": "enabled",
                    },
                },
            },
        ),
        _list,
    ))

    # --- playbook_run ---
    # plans/009: hybrid-async. The old tool awaited the whole run and hit its
    # 120s timeout on any slow playbook — the agent got a bare timeout, no
    # run_id, and the orphaned run kept executing invisibly. Now the run
    # starts in the background, we wait a bounded window, and either return
    # the finished results (fast playbooks: unchanged one-call UX) or the
    # run_id to poll with playbook_status.
    _RUN_WAIT_DEFAULT = 55.0
    _RUN_WAIT_MAX = 90.0

    async def _run(*, name: str, inputs: str = "{}", wait_seconds: float | None = None) -> str:
        try:
            input_data = json.loads(inputs) if isinstance(inputs, str) else inputs
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON inputs"})

        if wait_seconds is None:
            wait_seconds = _RUN_WAIT_DEFAULT
        wait_seconds = max(0.0, min(float(wait_seconds), _RUN_WAIT_MAX))

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()

        if not playbook:
            return json.dumps({"error": f"Playbook '{name}' not found"})

        if playbook.agent_autonomy in (
            AgentAutonomy.MANUAL_ONLY.value,
            AgentAutonomy.AGENT_MUST_CONFIRM.value,
        ):
            return json.dumps({
                "needs_approval": True,
                "playbook": name,
                "display_name": playbook.display_name,
                "description": playbook.description,
                "current_autonomy": playbook.agent_autonomy,
                "message": (
                    "This playbook requires owner approval. "
                    "DO NOT ask the user for permission — directly call "
                    "playbook_set_autonomy(name, 'agent_may_trigger') now. "
                    "The approval system will show a confirmation card to the "
                    "owner automatically. Once approved, retry playbook_run."
                ),
            })

        run = await runner.start_run_background(
            playbook, inputs=input_data, trigger="agent",
        )
        waited = await runner.wait_for_run(run.id, timeout=wait_seconds)
        status = waited.status if waited else run.status

        result: dict = {
            "run_id": str(run.id),
            "playbook": name,
            "status": status,
        }
        if playbook.candidate_version:
            result["note"] = (
                "This ran the LIVE version "
                f"({playbook.live_version or playbook.version}) — an "
                f"un-promoted candidate (v{playbook.candidate_version}) "
                "exists. Use playbook_run_candidate to test it, "
                "playbook_promote to make it live."
            )

        if status == "running":
            result["message"] = (
                "The playbook is still executing in the background (this is "
                f"normal for runs longer than {int(wait_seconds)}s). Poll "
                "playbook_status(run_id) to see step-by-step progress and "
                "final outputs. Do NOT re-run the playbook, and do NOT report "
                "results until playbook_status shows status 'done'."
            )
        elif status == "failed":
            result["error"] = (
                "Playbook execution FAILED. Do NOT fabricate results. "
                "Check the error details with playbook_status."
            )
        elif status == "done":
            async with session_factory() as session:
                steps = (await session.execute(
                    select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
                )).scalars().all()
                result["step_results"] = {
                    s.step_id: s.outputs for s in steps if s.outputs
                }
                if not result["step_results"]:
                    result["warning"] = (
                        "Playbook completed but produced no step outputs. "
                        "Verify the playbook has working steps before "
                        "reporting results to the user."
                    )

        return json.dumps(result)

    tools.append((
        ToolDef(
            name="playbook_run",
            # chat_only: an agent_step INSIDE a playbook must never trigger
            # playbooks (006.707: working prompt_sections made nested agents
            # see the playbook list and recursively self-trigger — 8 stacked
            # runs). Use a `subtask` step for playbook composition.
            chat_only=True,
            timeout_seconds=120,
            description=(
                "Trigger a playbook run. The run executes in the BACKGROUND: "
                "this returns the run_id immediately and waits up to "
                "wait_seconds (default 55) for completion. Fast playbooks "
                "return their results directly (status 'done' + step_results). "
                "If the result says status 'running', the playbook is still "
                "going — poll playbook_status(run_id) until it reaches "
                "'done'/'failed'; never re-run it and never invent results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "inputs": {"type": "string", "description": "JSON string of inputs"},
                    "wait_seconds": {
                        "type": "number",
                        "description": (
                            "How long to wait for completion before returning "
                            "(0–90, default 55). Use 0 to fire-and-forget and "
                            "poll playbook_status yourself."
                        ),
                    },
                },
                "required": ["name"],
            },
        ),
        _run,
    ))

    # --- playbook_status ---
    async def _status(*, run_id: str) -> str:
        async with session_factory() as session:
            run = await session.get(PlaybookRun, uuid.UUID(run_id))
            if not run:
                return json.dumps({"error": "Run not found"})

            steps = (await session.execute(
                select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
            )).scalars().all()

            # plans/009: the polling target for background runs — surface
            # run-level timing and the failing step's error at top level so a
            # polling agent doesn't have to dig for them.
            step_errors = [s.error for s in steps if s.error]
            payload: dict = {
                "run_id": run_id,
                "status": run.status,
                "trigger": run.trigger,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "steps": [{
                    "step_id": s.step_id,
                    "kind": s.step_kind,
                    "status": s.status,
                    "inputs": s.inputs,
                    "outputs": s.outputs,
                    "error": s.error,
                } for s in steps],
            }
            if step_errors:
                payload["error"] = step_errors[-1]
            if run.status == "running":
                payload["hint"] = (
                    "Still running — poll playbook_status again in a bit. "
                    "Completed steps above already show their outputs."
                )
            return json.dumps(payload)

    tools.append((
        ToolDef(
            name="playbook_status",
            description=(
                "Get the live state of a playbook run: overall status "
                "(running/done/failed/cancelled), timing, and the full "
                "step-by-step trace with each step's outputs and errors. "
                "Poll this after playbook_run returns status 'running'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run UUID"},
                },
                "required": ["run_id"],
            },
        ),
        _status,
    ))

    # --- playbook_cancel ---
    async def _cancel(*, run_id: str) -> str:
        await runner.cancel_run(uuid.UUID(run_id))
        return json.dumps({"run_id": run_id, "status": "cancelled"})

    tools.append((
        ToolDef(
            name="playbook_cancel",
            description="Cancel a running playbook.",
            parameters={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run UUID"},
                },
                "required": ["run_id"],
            },
        ),
        _cancel,
    ))

    # --- playbook_set_autonomy ---
    async def _set_autonomy(*, name: str, agent_autonomy: str) -> str:
        valid = {e.value for e in AgentAutonomy}
        if agent_autonomy not in valid:
            return json.dumps({"error": f"Invalid autonomy: {agent_autonomy}. Valid: {sorted(valid)}"})

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            old = playbook.agent_autonomy
            playbook.agent_autonomy = agent_autonomy
            await session.commit()
        return json.dumps({
            "playbook": name,
            "old_autonomy": old,
            "new_autonomy": agent_autonomy,
            "status": "updated",
        })

    tools.append((
        ToolDef(
            name="playbook_set_autonomy",
            chat_only=True,
            description=(
                "Change who can trigger a playbook. Use this when the owner wants to "
                "allow or restrict agent execution of a specific playbook. "
                "Options: 'agent_may_trigger' (agent runs freely), "
                "'agent_must_confirm' (agent must ask first), "
                "'manual_only' (agent cannot run it at all)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "agent_autonomy": {
                        "type": "string",
                        "enum": ["agent_may_trigger", "agent_must_confirm", "manual_only"],
                        "description": "The new autonomy level",
                    },
                },
                "required": ["name", "agent_autonomy"],
            },
            policy="prompt_always",
            risk_level="medium",
        ),
        _set_autonomy,
    ))

    # --- Whole-YAML authoring helpers + tools ---

    async def _snapshot_version(
        session: AsyncSession,
        playbook: Playbook,
        *,
        author: str = "agent",
        message: str = "",
        promoted_from: int | None = None,
    ) -> PlaybookVersion:
        """Snapshot the current playbook definition into playbook_versions."""
        v = PlaybookVersion(
            playbook_id=playbook.id,
            version=playbook.version,
            definition=playbook.definition,
            code=playbook.code,
            manifest=playbook.manifest,
            author=author,
            message=message,
            promoted_from=promoted_from,
        )
        session.add(v)
        return v

    # 0.10.0 (plans/002 phase 3): candidate/live plumbing. `playbooks.version`
    # is the monotonic counter; live content stays on the playbook row
    # (version `live_version`); the one un-promoted candidate lives in a
    # playbook_versions row pointed at by `candidate_version`. A version row
    # holds the content OF that version number — which is what the historical
    # "snapshot before change" rows already held; only the current live
    # version may lack a row on legacy playbooks, hence _ensure_live_row.

    def _live_version_of(playbook: Playbook) -> int:
        return playbook.live_version or playbook.version

    async def _get_version_row(
        session: AsyncSession, playbook: Playbook, n: int,
    ) -> PlaybookVersion | None:
        return (await session.execute(
            select(PlaybookVersion).where(
                PlaybookVersion.playbook_id == playbook.id,
                PlaybookVersion.version == n,
            )
        )).scalar_one_or_none()

    async def _ensure_live_row(
        session: AsyncSession, playbook: Playbook,
    ) -> PlaybookVersion:
        """Guarantee a version row exists for the current live content."""
        n = _live_version_of(playbook)
        row = await _get_version_row(session, playbook, n)
        if row is None:
            row = PlaybookVersion(
                playbook_id=playbook.id,
                version=n,
                definition=playbook.definition,
                code=playbook.code,
                manifest=playbook.manifest,
                author="system",
                message="live content (recorded on first candidate/promote)",
            )
            session.add(row)
        return row

    def _version_code(row: PlaybookVersion) -> str:
        """pblang source of a version row (stored, or derived on read)."""
        if row.code:
            return row.code
        return generate_code(PlaybookDef.model_validate(row.definition))

    def _apply_version_to_live(
        playbook: Playbook, row: PlaybookVersion, *, restore_manifest: bool,
    ) -> None:
        """Make a version row's content the live content (pointer + fields)."""
        defn = dict(row.definition)
        defn["name"] = playbook.name  # never rename via promote/rollback
        playbook.definition = defn
        playbook.code = row.code
        if restore_manifest:
            playbook.manifest = row.manifest
        playbook.description = defn.get("description") or playbook.description
        playbook.when_to_use = defn.get("when_to_use") or playbook.when_to_use
        playbook.display_name = defn.get("display_name") or playbook.display_name
        playbook.inputs_schema = defn.get("inputs")
        playbook.live_version = row.version

    def _shim_playbook(playbook: Playbook, row: PlaybookVersion) -> Playbook:
        """Transient Playbook carrying a version row's content — NEVER added
        to a session. Lets the runner execute/dry-run a candidate untouched
        (it only reads id/name/display_name/definition/live_version)."""
        return Playbook(
            id=playbook.id,
            name=playbook.name,
            display_name=playbook.display_name,
            description=playbook.description,
            when_to_use=playbook.when_to_use,
            inputs_schema=dict(row.definition).get("inputs"),
            definition=row.definition,
            code=row.code,
            manifest=row.manifest,
            version=row.version,
            live_version=row.version,
            status=playbook.status,
            agent_autonomy=playbook.agent_autonomy,
        )

    async def _playbook_get_definition(*, name: str, format: str = "code") -> str:
        import yaml as _yaml

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})

            if format == "yaml":
                return _yaml.dump(
                    playbook.definition, default_flow_style=False, sort_keys=False,
                )
            try:
                return _derive_code(playbook)
            except Exception as e:  # noqa: BLE001 — legacy defs must stay readable
                return json.dumps({
                    "error": f"Could not render code for '{name}': {e}",
                    "hint": "retry with format='yaml'",
                })

    tools.append((
        ToolDef(
            name="playbook_get_definition",
            description=(
                "Get a playbook's full source so you can edit it. Returns the "
                "playbook CODE (the Python-like playbook language) by default — "
                "edit it and pass it back via playbook_edit(code=...), or make a "
                "targeted change with playbook_edit(old=..., new=...). "
                "format='yaml' returns the raw YAML IR instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "format": {
                        "type": "string",
                        "enum": ["code", "yaml"],
                        "default": "code",
                    },
                },
                "required": ["name"],
            },
        ),
        _playbook_get_definition,
    ))

    # --- playbook_validate (the compiler) ---
    async def _validate(*, name: str = "", definition_yaml: str = "", code: str = "") -> str:
        import yaml as _yaml

        check_keys = False
        if code:
            pb_def, err = _compile_code(code, name=name or "unnamed")
            if err:
                payload = json.loads(err)
                return json.dumps({
                    "ok": False,
                    "errors": payload["issues"],
                    "warnings": [],
                    "saved": False,
                    "note": "Compile errors — nothing was checked further.",
                })
            defn = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
            # compiler already rejects unknown kwargs; the dump carries
            # cross-kind defaults the key checker would falsely flag.
            check_keys = False
        elif definition_yaml:
            try:
                defn: Any = _yaml.safe_load(definition_yaml)
            except Exception as e:
                return json.dumps({
                    "ok": False,
                    "errors": [{"severity": "error", "message": f"YAML: {e}"}],
                })
            if not isinstance(defn, dict):
                return json.dumps({
                    "ok": False,
                    "errors": [{"severity": "error", "message": "YAML must be a mapping"}],
                })
            check_keys = True
        elif name:
            async with session_factory() as session:
                pb = (await session.execute(
                    select(Playbook).where(Playbook.name == name)
                )).scalar_one_or_none()
            if not pb:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            defn = pb.definition
        else:
            return json.dumps({"error": "Provide 'name', 'code', or 'definition_yaml'."})

        async with session_factory() as session:
            all_pb = await _load_all_playbook_steps(session, exclude=name or None)
        issues = validate_definition(
            defn, tool_registry=getattr(runner, "_tools", None), all_playbooks=all_pb,
            check_unknown_keys=check_keys,
        )
        errors = [i.to_dict() for i in issues if i.severity == "error"]
        warnings = [i.to_dict() for i in issues if i.severity == "warning"]
        # 0.6.0 (luna 074/phase4): validate returns a success-shaped payload
        # ("ok": true) that headless agents repeatedly mistook for a completed
        # save — validate and edit have near-identical schemas, and edit used
        # to be invisible headless. Say explicitly that nothing was persisted.
        return json.dumps({
            "ok": not errors, "errors": errors, "warnings": warnings,
            "saved": False,
            "note": (
                "Validation only — NOTHING was saved. To persist a change, "
                "call playbook_edit (existing playbook) or playbook_propose "
                "(new playbook)."
            ),
        })

    tools.append((
        ToolDef(
            name="playbook_validate",
            description=(
                "Statically check a playbook WITHOUT running it (the compiler). "
                "Returns ALL issues at once: compile errors, schema errors, unknown "
                "keys, undefined {{inputs}}/{{steps}} references, use-before-define, "
                "bad loops, unknown tools, subtask cycles, and context-economy "
                "warnings. Pass a saved playbook 'name', playbook 'code' "
                "(preferred), or a 'definition_yaml'. Run this before saving or "
                "running."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Saved playbook name"},
                    "code": {"type": "string", "description": "Full playbook code to check"},
                    "definition_yaml": {"type": "string", "description": "Full YAML to check"},
                },
            },
            policy="auto_approve",
            risk_level="low",
        ),
        _validate,
    ))

    # --- playbook_dry_run (the test harness) ---
    async def _dry_run(*, name: str, inputs: str = "{}", version: str = "auto") -> str:
        try:
            input_data = json.loads(inputs) if isinstance(inputs, str) else inputs
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON inputs"})

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})

            # 0.10.0: default to the candidate when one exists — dry-running
            # the thing you just edited is the point of the flow.
            target = playbook
            tested = _live_version_of(playbook)
            want = (version or "auto").strip().lower()
            if want == "auto":
                want = "candidate" if playbook.candidate_version else "live"
            if want == "candidate":
                if not playbook.candidate_version:
                    return json.dumps({
                        "error": f"'{name}' has no candidate — save an edit "
                                 "first, or dry-run version='live'.",
                    })
                row = await _get_version_row(
                    session, playbook, playbook.candidate_version,
                )
                if row is None:
                    return json.dumps({
                        "error": "Candidate version row is missing (corrupt "
                                 "state) — save the edit again.",
                    })
                target = _shim_playbook(playbook, row)
                tested = row.version
            elif want != "live":
                try:
                    n = int(want)
                except ValueError:
                    return json.dumps({
                        "error": "version must be 'auto', 'candidate', "
                                 "'live', or a version number.",
                    })
                if n == _live_version_of(playbook):
                    pass  # live content lives on the playbook row itself
                else:
                    row = await _get_version_row(session, playbook, n)
                    if row is None:
                        return json.dumps({
                            "error": f"No stored content for version {n}.",
                        })
                    target = _shim_playbook(playbook, row)
                    tested = n

        trace = await runner.dry_run(target, inputs=input_data)
        if isinstance(trace, dict):
            trace["tested_version"] = tested
            trace["is_candidate"] = bool(
                playbook.candidate_version and tested == playbook.candidate_version
            )
        return json.dumps(trace)

    tools.append((
        ToolDef(
            name="playbook_dry_run",
            chat_only=True,
            timeout_seconds=60,
            description=(
                "Simulate a playbook run WITHOUT side effects — real loops, "
                "conditions, branches, and templates, but tool/LLM/wait steps are "
                "stubbed. Returns a trace of resolved args, branches taken, and loop "
                "iterations. Use to test logic before a real run. The outputs are "
                "SIMULATED — never report them to the user as real results. "
                "Tests the CANDIDATE version by default when one exists "
                "(version='live' or a number overrides)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "inputs": {"type": "string", "description": "JSON string of inputs"},
                    "version": {
                        "type": "string",
                        "description": (
                            "'auto' (default: candidate if one exists, else "
                            "live), 'candidate', 'live', or a version number."
                        ),
                    },
                },
                "required": ["name"],
            },
        ),
        _dry_run,
    ))

    # --- playbook_edit (staged: read → ticket → write) ---
    # 0.9.0 (plans/002 phase 2): the flow lives in the tool layer, not prose
    # (memory: flows-belong-in-tool-layer). Calling with no payload is the
    # READ stage (manifest + code + single-use ticket); the WRITE stage
    # requires that ticket, so the agent has provably seen the manifest and
    # the current source before saving.

    async def _drift_check(
        manifest: str, old_code: str, new_code: str,
    ) -> tuple[dict | None, str | None]:
        """(verdict, warning). verdict={'conflict','reason'} or None when the
        check could not run — fail OPEN with the warning (an LLM outage must
        not brick editing)."""
        agent = getattr(runner, "_agent", None)
        if agent is None:
            return None, None
        prompt = (
            "A playbook is about to be edited. Its MANIFEST states the "
            "owner's intent: purpose, side effects, invariants. Decide "
            "whether the NEW CODE conflicts with the manifest — it removes "
            "or changes behavior the manifest promises, or adds behavior "
            "the manifest forbids. Refactors, cosmetic changes, and "
            "additions the manifest does not address are NOT conflicts.\n\n"
            f"MANIFEST:\n{manifest}\n\n"
            f"OLD CODE:\n{old_code}\n\n"
            f"NEW CODE:\n{new_code}\n"
        )
        try:
            result, _usage = await agent.run_llm(
                prompt,
                purpose="summarization",
                system=(
                    "You check playbook edits against their owner-stated "
                    "manifest. Flag only real conflicts with what the "
                    "manifest says; when in doubt, no conflict."
                ),
                output_schema={"conflict": "bool", "reason": "str"},
            )
        except Exception as e:  # noqa: BLE001 — fail open
            return None, f"Manifest drift check unavailable ({e}); edit allowed."
        if isinstance(result, dict) and isinstance(result.get("conflict"), bool):
            return (
                {"conflict": result["conflict"],
                 "reason": str(result.get("reason", ""))},
                None,
            )
        return None, "Manifest drift check gave an unusable answer; edit allowed."

    async def _edit_impl(
        *,
        name: str,
        ticket: str = "",
        code: str = "",
        old: str = "",
        new: str = "",
        definition_yaml: str = "",
        skip_drift: bool = False,
        forced: bool = False,
    ) -> str:
        snippet_mode = bool(old) or bool(new)
        modes = sum([bool(code), snippet_mode, bool(definition_yaml)])

        # READ stage: no payload at all → manifest + code + fresh ticket.
        if modes == 0:
            async with session_factory() as session:
                playbook = (await session.execute(
                    select(Playbook).where(Playbook.name == name)
                )).scalar_one_or_none()
                if not playbook:
                    return json.dumps({
                        "error": f"Playbook '{name}' not found. Use "
                                 "playbook_propose to create it.",
                    })
                # 0.10.0: when a candidate exists you iterate ON the
                # candidate — the read stage hands out its code, not live's.
                cand_row = None
                if playbook.candidate_version:
                    cand_row = await _get_version_row(
                        session, playbook, playbook.candidate_version,
                    )
                try:
                    current = _version_code(cand_row) if cand_row else _derive_code(playbook)
                except Exception:  # noqa: BLE001 — legacy defs must stay editable
                    current = ""
                t = await _issue_ticket(session, playbook)
                payload = {
                    "stage": "read",
                    "editing": "candidate" if cand_row else "live",
                    "manifest": playbook.manifest,
                    "code": current,
                    "version": playbook.version,
                    "live_version": _live_version_of(playbook),
                    "candidate_version": playbook.candidate_version,
                    "ticket": str(t.id),
                    "expires_in_seconds": _TICKET_TTL_SECONDS,
                    "instructions": (
                        "Read the manifest and the current code above — your "
                        "edit must stay within what the manifest states. Then "
                        "call playbook_edit again with this ticket and exactly "
                        "one of: code= (full source), old=/new= (targeted "
                        "snippet), or definition_yaml= (legacy). The ticket "
                        "is single-use and expires. Saving creates a CANDIDATE "
                        "— the live playbook keeps running unchanged until "
                        "playbook_promote."
                    ),
                }
                if not playbook.manifest:
                    payload["manifest_note"] = (
                        "This playbook has no manifest yet. Consider "
                        "proposing one to the owner via playbook_manifest_set."
                    )
                await session.commit()
            return json.dumps(payload)

        if modes != 1:
            return json.dumps({
                "error": "Provide exactly one of: 'code', 'old'+'new', or "
                         "'definition_yaml'.",
            })
        if snippet_mode and not (old and new is not None):
            return json.dumps({"error": "Snippet edits need both 'old' and 'new'."})

        # WRITE stage, part 1: ticket check + compile + validate (no lock —
        # the LLM drift call must not hold a row lock or an open session).
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({
                    "error": f"Playbook '{name}' not found. Use playbook_propose to create it.",
                })
            refusal = await _check_ticket(session, playbook, ticket, consume=False)
            if refusal:
                return json.dumps({"error": refusal})
            base_version = playbook.version
            manifest = playbook.manifest
            # Edits build on the candidate when one exists (that's what the
            # read stage handed out), else on live.
            cand_row = None
            if playbook.candidate_version:
                cand_row = await _get_version_row(
                    session, playbook, playbook.candidate_version,
                )
            try:
                old_code = _version_code(cand_row) if cand_row else _derive_code(playbook)
            except Exception:  # noqa: BLE001
                old_code = ""

            stored_code: str | None
            if snippet_mode:
                if not old_code:
                    return json.dumps({
                        "error": f"Cannot snippet-edit '{name}': its code "
                                 "cannot be rendered. Use code= with the "
                                 "full source instead.",
                    })
                count = old_code.count(old)
                if count == 0:
                    return json.dumps({
                        "error": "The 'old' snippet was not found in the "
                                 "current code. Use the code returned by the "
                                 "read stage and copy the exact text.",
                    })
                if count > 1:
                    return json.dumps({
                        "error": f"The 'old' snippet matches {count} places — "
                                 "include more surrounding context so it is "
                                 "unique.",
                    })
                code = old_code.replace(old, new)

            if code:
                pb_def, err = _compile_code(code, name=name)
                if err:
                    return err
                stored_code = code
                check_target: Any = pb_def.model_dump(
                    mode="json", exclude_none=True, by_alias=True,
                )
            else:
                try:
                    pb_def = parse_yaml(definition_yaml)
                except Exception as e:
                    return json.dumps({"error": f"Invalid YAML: {e}"})
                stored_code = _codegen_or_none(pb_def)
                import yaml as _yaml
                check_target = _yaml.safe_load(definition_yaml)

            all_pb = await _load_all_playbook_steps(session, exclude=name)
            issues = validate_definition(
                check_target,
                tool_registry=getattr(runner, "_tools", None), all_playbooks=all_pb,
                # compiled dumps carry cross-kind defaults the key checker
                # would falsely flag; the compiler already rejects typos.
                check_unknown_keys=bool(definition_yaml),
            )
            errors = [i.to_dict() for i in issues if i.severity == "error"]
            if errors:
                return json.dumps({
                    "error": "Edit rejected — the new definition is invalid.",
                    "issues": errors,
                })

        # Drift gate: only when a manifest exists. Refusal does NOT burn the
        # ticket — fix the code and retry with the same one.
        drift_warning: str | None = None
        if manifest.strip() and not skip_drift:
            verdict, drift_warning = await _drift_check(
                manifest, old_code, stored_code or definition_yaml,
            )
            if verdict and verdict["conflict"]:
                return json.dumps({
                    "error": "Edit refused — it conflicts with the playbook's manifest.",
                    "reason": verdict["reason"],
                    "your_options": [
                        "Change the code so it stays within the manifest, "
                        "then retry with the SAME ticket (still valid until "
                        "it expires).",
                        "If the manifest itself is outdated, update it with "
                        "playbook_manifest_set (asks the owner for approval), "
                        "then retry the edit.",
                        "If the owner explicitly wants this change anyway, "
                        "use playbook_edit_force (also asks the owner for "
                        "approval).",
                    ],
                })

        # WRITE stage, part 2: consume the ticket and save, under lock.
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            if playbook.version != base_version:
                return json.dumps({
                    "error": "The playbook changed while you were editing. "
                             "Call playbook_edit(name) to re-read and get a "
                             "fresh ticket.",
                })
            refusal = await _check_ticket(session, playbook, ticket, consume=True)
            if refusal:
                return json.dumps({"error": refusal})

            # 0.10.0: a save creates a CANDIDATE version row — live content
            # on the playbook row is not touched. One candidate max: the
            # pointer moves, the previous candidate row stays in history.
            await _ensure_live_row(session, playbook)
            data = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
            data["name"] = name  # never rename via edit
            playbook.version += 1
            session.add(PlaybookVersion(
                playbook_id=playbook.id,
                version=playbook.version,
                definition=data,
                code=stored_code,
                manifest=playbook.manifest,
                author="agent",
                message=(
                    "candidate (drift gate skipped — forced edit)"
                    if forced else "candidate"
                ),
            ))
            playbook.candidate_version = playbook.version
            await session.commit()
            new_version = playbook.version
            live_version = _live_version_of(playbook)

        await events.emit("playbook.candidate.saved", {
            "name": name, "candidate_version": new_version,
        })
        warnings = [i.to_dict() for i in issues if i.severity == "warning"]
        result: dict[str, Any] = {
            "playbook": name,
            "status": "candidate_saved",
            "candidate_version": new_version,
            "live_version": live_version,
            "warnings": warnings,
            "next": (
                "The LIVE playbook is unchanged — triggers and playbook_run "
                "still execute version "
                f"{live_version}. Test the candidate with playbook_dry_run "
                "(it targets the candidate by default), then call "
                "playbook_promote(name) to make it live. playbook_rollback "
                "restores the previous live version after a promote."
            ),
        }
        if forced:
            result["note"] = "manifest drift gate skipped (forced edit)"
        if drift_warning:
            result["drift_warning"] = drift_warning
        return json.dumps(result)

    async def _playbook_edit(
        *,
        name: str,
        ticket: str = "",
        code: str = "",
        old: str = "",
        new: str = "",
        definition_yaml: str = "",
    ) -> str:
        return await _edit_impl(
            name=name, ticket=ticket, code=code, old=old, new=new,
            definition_yaml=definition_yaml,
        )

    _EDIT_PAYLOAD_PROPS = {
        "name": {"type": "string", "description": "Existing playbook name"},
        "ticket": {
            "type": "string",
            "description": "Edit ticket from the read stage (required to save)",
        },
        "code": {"type": "string", "description": "Full new playbook code"},
        "old": {
            "type": "string",
            "description": "Exact snippet of the current code to replace (must be unique)",
        },
        "new": {"type": "string", "description": "Replacement text for 'old'"},
        "definition_yaml": {
            "type": "string",
            "description": "Full new YAML definition (legacy format)",
        },
    }

    tools.append((
        ToolDef(
            name="playbook_edit",
            # 0.6.0 (luna 074/phase4): no longer chat_only. Headless turns
            # (scheduled fires, playbook agent_steps) could only reach
            # playbook_validate — the no-op twin with a near-identical schema
            # — so scheduled "update the playbook" tasks silently saved
            # nothing. Headless tool calls go through the same dispatch/
            # approval gate as chat since luna 0.40.003, and the edit
            # validates + snapshots a version before replacing.
            description=(
                "Change an existing playbook — a two-step flow. STEP 1 (read): "
                "call with ONLY the name; you get the playbook's manifest (its "
                "owner-stated intent), the current code, and a single-use edit "
                "ticket. STEP 2 (write): call again with that ticket plus "
                "exactly one of code= (full new source), old=/new= (targeted "
                "snippet; 'old' must match exactly one place), or "
                "definition_yaml= (legacy). The write compiles, validates, "
                "checks the edit against the manifest, snapshots a version, "
                "then replaces the definition. Edits that conflict with the "
                "manifest are refused."
            ),
            parameters={
                "type": "object",
                "properties": _EDIT_PAYLOAD_PROPS,
                "required": ["name"],
            },
        ),
        _playbook_edit,
    ))

    # --- playbook_edit_force (drift override — owner approval) ---
    async def _playbook_edit_force(
        *,
        name: str,
        ticket: str = "",
        code: str = "",
        old: str = "",
        new: str = "",
        definition_yaml: str = "",
    ) -> str:
        return await _edit_impl(
            name=name, ticket=ticket, code=code, old=old, new=new,
            definition_yaml=definition_yaml, skip_drift=True, forced=True,
        )

    tools.append((
        ToolDef(
            name="playbook_edit_force",
            description=(
                "Save a playbook edit even though it conflicts with the "
                "playbook's manifest. Same arguments and ticket flow as "
                "playbook_edit; the manifest drift gate is skipped and the "
                "version history records the override. Use ONLY after "
                "playbook_edit refused for manifest conflict AND the owner "
                "wants the change anyway — this raises an approval card."
            ),
            parameters={
                "type": "object",
                "properties": _EDIT_PAYLOAD_PROPS,
                "required": ["name", "ticket"],
            },
            policy="prompt_always",
            risk_level="medium",
        ),
        _playbook_edit_force,
    ))

    # --- playbook_manifest_set (owner approval) ---
    async def _manifest_set(*, name: str, manifest: str) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            # 0.10.0: the manifest is LIVE content. Record the old live
            # version, then create a new live version row carrying the new
            # manifest. (Never snapshot at the counter — a pending candidate
            # already owns that number, and version numbers must stay unique.)
            await _ensure_live_row(session, playbook)
            playbook.manifest = manifest
            playbook.version += 1
            session.add(PlaybookVersion(
                playbook_id=playbook.id,
                version=playbook.version,
                definition=playbook.definition,
                code=playbook.code,
                manifest=manifest,
                author="agent",
                message="manifest updated",
            ))
            playbook.live_version = playbook.version
            await session.commit()
            new_version = playbook.version
        await events.emit("playbook.saved", {"name": name})
        return json.dumps({
            "playbook": name, "version": new_version, "status": "manifest_set",
            "manifest_chars": len(manifest),
        })

    tools.append((
        ToolDef(
            name="playbook_manifest_set",
            description=(
                "Set or replace a playbook's MANIFEST — the owner-stated "
                "intent in plain markdown: Purpose, Side effects, Never "
                "(invariants), Acceptance. Future edits are checked against "
                "it and refused when they conflict, so changing it is an "
                "owner decision — this raises an approval card. Draft the "
                "manifest from what the owner said and from the playbook's "
                "code; keep it short and testable."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "manifest": {
                        "type": "string",
                        "description": "Full manifest text (markdown). Replaces the current one.",
                    },
                },
                "required": ["name", "manifest"],
            },
            policy="prompt_always",
            risk_level="medium",
        ),
        _manifest_set,
    ))

    # --- playbook_promote (the gate: candidate → live) ---
    # 0.10.0 (plans/002 phase 3): promotion runs an extensible gate list —
    # static validation and manifest drift today; specs (phase 4) and probes
    # (phase 5) plug in as new entries. A refusal names the failing gate.
    async def _promote(*, name: str) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            if not playbook.candidate_version:
                return json.dumps({
                    "error": f"'{name}' has no candidate to promote. Save an "
                             "edit first (playbook_edit).",
                })
            row = await _get_version_row(
                session, playbook, playbook.candidate_version,
            )
            if row is None:
                return json.dumps({
                    "error": "Candidate version row is missing (corrupt "
                             "state) — save the edit again.",
                })

            gates: list[dict[str, Any]] = []
            # gate 1: static validation of the candidate definition.
            all_pb = await _load_all_playbook_steps(session, exclude=name)
            issues = validate_definition(
                row.definition,
                tool_registry=getattr(runner, "_tools", None),
                all_playbooks=all_pb,
                check_unknown_keys=False,
            )
            errors = [i.to_dict() for i in issues if i.severity == "error"]
            gates.append({"gate": "static_validation", "ok": not errors})
            if errors:
                return json.dumps({
                    "error": "Promote refused — gate 'static_validation' failed.",
                    "gate": "static_validation",
                    "issues": errors,
                    "hint": "Fix the candidate via playbook_edit and retry.",
                })
            # gate 2: manifest drift — candidates only exist because the save
            # passed the drift check or the owner approved a forced edit
            # (recorded on the version row). Reported, never re-run here.
            forced = "forced" in (row.message or "")
            gates.append({
                "gate": "manifest_drift", "ok": True,
                "note": (
                    "owner-approved forced edit" if forced
                    else "checked at save time"
                ),
            })
            # (specs and probes gates plug in here — phases 4 and 5.)

            old_live = _live_version_of(playbook)
            await _ensure_live_row(session, playbook)
            _apply_version_to_live(playbook, row, restore_manifest=False)
            row.promoted_from = old_live  # rollback lineage
            playbook.candidate_version = None
            new_live = playbook.live_version
            await session.commit()

        # live content changed — resync triggers and refresh the canvas.
        await events.emit("playbook.saved", {"name": name})
        await events.emit("ui.plugin.event", {
            "plugin": "plugin-playbooks",
            "event": "playbook.patch",
            "payload": {"draft_id": name, "action": "replace", "name": name},
            "focus": True,
        })
        return json.dumps({
            "playbook": name,
            "status": "promoted",
            "live_version": new_live,
            "previous_live_version": old_live,
            "gates": gates,
            "note": (
                "The candidate is now LIVE — triggers and playbook_run "
                "execute it. playbook_rollback(name) restores version "
                f"{old_live} if it misbehaves."
            ),
        })

    tools.append((
        ToolDef(
            name="playbook_promote",
            description=(
                "Make a playbook's CANDIDATE version live. Runs the promotion "
                "gate first (static validation; manifest drift was enforced "
                "at save time) and refuses naming the failing gate. Until "
                "this succeeds, triggers and playbook_run keep executing the "
                "old live version. Test the candidate first: playbook_dry_run "
                "targets it by default."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                },
                "required": ["name"],
            },
            policy="prompt_always",
            risk_level="medium",
        ),
        _promote,
    ))

    # --- playbook_rollback (live ← previous live) ---
    async def _rollback(*, name: str) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            live_n = _live_version_of(playbook)
            live_row = await _get_version_row(session, playbook, live_n)
            target_n = live_row.promoted_from if live_row else None
            if not target_n:
                # legacy lineage: rows below live are plain history — take
                # the newest one.
                from sqlalchemy import func
                target_n = (await session.execute(
                    select(func.max(PlaybookVersion.version)).where(
                        PlaybookVersion.playbook_id == playbook.id,
                        PlaybookVersion.version < live_n,
                    )
                )).scalar()
            if not target_n:
                return json.dumps({
                    "error": f"'{name}' has no previous version to roll back to.",
                })
            row = await _get_version_row(session, playbook, target_n)
            if row is None:
                return json.dumps({
                    "error": f"No stored content for version {target_n} — "
                             "cannot roll back.",
                })
            await _ensure_live_row(session, playbook)
            _apply_version_to_live(playbook, row, restore_manifest=True)
            await session.commit()

        await events.emit("playbook.saved", {"name": name})
        await events.emit("ui.plugin.event", {
            "plugin": "plugin-playbooks",
            "event": "playbook.patch",
            "payload": {"draft_id": name, "action": "replace", "name": name},
            "focus": True,
        })
        return json.dumps({
            "playbook": name,
            "status": "rolled_back",
            "live_version": target_n,
            "previous_live_version": live_n,
            "note": (
                f"Version {target_n} is live again (manifest included). "
                f"Version {live_n} stays in history — playbook_promote a new "
                "candidate to move forward."
            ),
        })

    tools.append((
        ToolDef(
            name="playbook_rollback",
            description=(
                "Restore a playbook's PREVIOUS live version (the one the "
                "current live was promoted from). Use when a promoted change "
                "misbehaves. The rolled-back-from version stays in history."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                },
                "required": ["name"],
            },
            policy="prompt_always",
            risk_level="medium",
        ),
        _rollback,
    ))

    # --- playbook_run_candidate (supervised real test run) ---
    async def _run_candidate(
        *, name: str, inputs: str = "{}", wait_seconds: float | None = None,
    ) -> str:
        try:
            input_data = json.loads(inputs) if isinstance(inputs, str) else inputs
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON inputs"})
        if wait_seconds is None:
            wait_seconds = _RUN_WAIT_DEFAULT
        wait_seconds = max(0.0, min(float(wait_seconds), _RUN_WAIT_MAX))

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            if not playbook.candidate_version:
                return json.dumps({
                    "error": f"'{name}' has no candidate — save an edit "
                             "first, or use playbook_run for the live version.",
                })
            row = await _get_version_row(
                session, playbook, playbook.candidate_version,
            )
            if row is None:
                return json.dumps({
                    "error": "Candidate version row is missing (corrupt "
                             "state) — save the edit again.",
                })
            shim = _shim_playbook(playbook, row)
            candidate_version = row.version

        run = await runner.start_run_background(
            shim, inputs=input_data, trigger="agent-candidate",
        )
        waited = await runner.wait_for_run(run.id, timeout=wait_seconds)
        status = waited.status if waited else run.status

        result: dict[str, Any] = {
            "run_id": str(run.id),
            "playbook": name,
            "candidate_version": candidate_version,
            "status": status,
            "note": (
                "This was a REAL run of the CANDIDATE (side effects "
                "included). The live playbook is unchanged — call "
                "playbook_promote when satisfied."
            ),
        }
        if status == "running":
            result["message"] = (
                "Still executing in the background — poll "
                "playbook_status(run_id) until 'done'/'failed'. Do NOT "
                "re-run, do NOT report results yet."
            )
        elif status == "failed":
            result["error"] = (
                "Candidate run FAILED. Do NOT fabricate results and do NOT "
                "promote. Check playbook_status for the error details."
            )
        elif status == "done":
            async with session_factory() as session:
                steps = (await session.execute(
                    select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
                )).scalars().all()
                result["step_results"] = {
                    s.step_id: s.outputs for s in steps if s.outputs
                }
        return json.dumps(result)

    tools.append((
        ToolDef(
            name="playbook_run_candidate",
            chat_only=True,
            timeout_seconds=120,
            description=(
                "REAL, supervised test run of a playbook's CANDIDATE version "
                "— actual tools, actual side effects, recorded in run "
                "history against the candidate version number. The live "
                "playbook stays untouched. Prefer playbook_dry_run first; "
                "use this when the owner wants proof against real systems "
                "before playbook_promote."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "inputs": {"type": "string", "description": "JSON string of inputs"},
                    "wait_seconds": {
                        "type": "number",
                        "description": (
                            "How long to wait for completion before returning "
                            "(0–90, default 55)."
                        ),
                    },
                },
                "required": ["name"],
            },
            policy="prompt_always",
            risk_level="medium",
        ),
        _run_candidate,
    ))

    return tools
