"""Agent tools for the Playbooks plugin.

These are the tools Luna uses to propose, list, run, and manage playbooks.

Authoring is Python (the pblang playbook language) — whole-source or targeted
snippet edits, never piecemeal node surgery. `playbook_propose` creates from
full code; `playbook_edit` rewrites from full code or applies an old=/new=
snippet (snapshot → validate → replace). To change a playbook:
`playbook_get_definition` → edit the code → `playbook_edit`;
`playbook_validate` / `playbook_dry_run` to check before `playbook_run`.
plans/023: zero YAML — every input/output is Python code or JSON.
"""

from __future__ import annotations

import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from luna_sdk import EventBus, ToolDef

from .definition import AgentAutonomy, PlaybookDef
from .models import (
    Playbook,
    PlaybookEditTicket,
    PlaybookRun,
    PlaybookSpec,
    PlaybookStepRun,
    PlaybookVersion,
)
from .pblang import PlaybookCompileError, compile_playbook, generate_code
from .probes import preflight_note, run_preflight
from .publish import (
    announce_publish,
    ops_conversation_id,
    specs_gate,
    test_run_gate,
)
from .reference import LANGUAGE_CHEATSHEET, LANGUAGE_MINIREF
from .runner import active_run_id as _active_playbook_run
from .specs import parse_spec, parse_spec_batch, run_all_specs, spec_from_run
from .validation import validate_definition
from .versioning import ensure_live_row, mint_version, spec_source_version
from .versioning import get_version_row as _tolerant_get_version_row_fn

_log = logging.getLogger("luna.plugin.playbooks.agent_tools")


def _gate_owner_line(gate: dict[str, Any]) -> str:
    """021: one ✓/✗ bullet per gate on the approval card, in owner words
    (vocabulary rule — internal gate codes never reach the owner's eyes)."""
    g, ok, note = gate.get("gate"), gate.get("ok"), gate.get("note", "")
    if g == "static_validation":
        return "Structure check passed" if ok else "Structure check failed"
    if g == "specs":
        if note == "no specs defined":
            return "No tests defined"
        return f"Tests: {note}" if ok else f"Tests red: {note}"
    if g == "test_run":
        if ok:
            return "Test run: green"
        # plans/022 P1: a FAILED run and a MISSING run are different truths.
        if "FAILED" in note:
            return "Test run: latest run of this version FAILED"
        return "No test run of this version"
    if g == "probes":
        return (
            "Tools it uses are reachable" if ok
            else f"A tool it uses is broken — {note}"
        )
    return f"{g}: {'ok' if ok else 'failed'}"


def _nested_run_refusal() -> str | None:
    """Refuse starting a playbook run from INSIDE a playbook run.

    006.707: nested agent_step turns that could see the run tools recursively
    self-triggered (8 stacked runs). chat_only used to hide the tools from
    every headless turn, but 0.31.1 removed it so muted ops wake turns can
    test candidates — this contextvar guard is the substitute, refusing only
    the actually-recursive context instead of all headless turns.
    """
    rid = _active_playbook_run()
    if rid is None:
        return None
    return json.dumps({
        "gate": "nested_playbook_run",
        "error": (
            f"Refused: this turn is a step inside playbook run {rid} — "
            "starting another playbook run from here would recurse."
        ),
        "hint": "To compose playbooks, use a `subtask` step in the "
                "playbook definition instead of calling run tools "
                "from an agent_step.",
    })


def _compile_code(code: str, *, name: str) -> tuple[PlaybookDef | None, str | None]:
    """(def, None) on success, (None, json error payload) on compile errors."""
    try:
        return compile_playbook(code, name=name), None
    except PlaybookCompileError as e:
        return None, json.dumps({
            "error": "The playbook code does not compile — fix these and retry.",
            "issues": [i.to_dict() for i in e.issues],
            # plans/003 phase 4: a compile error is where a syntax-guessing
            # agent re-enters — hand it the spec instead of another cycle.
            "language_reference": LANGUAGE_CHEATSHEET,
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
    a compile error must NOT burn the ticket).
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
    ctx: Any = None,
) -> list[tuple[ToolDef, Any]]:
    """Return (ToolDef, handler) pairs for all playbook agent tools.

    0.26.0 (plans/015, 089): `ctx` (PluginContext, optional for tests) feeds
    the publish path — ops-chat announcements and, on 089-capable cores, the
    conversation kind/state accessors.
    """

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
        # 0.14.0 (plans/002 phase 7): code is the ONLY authoring format.
        # definition_yaml is still a declared-nowhere kwarg so stale callers
        # get a steering hint instead of a TypeError.
        if definition_yaml:
            return json.dumps({
                "error": "YAML authoring was removed — write the playbook as "
                         "`code` (see the playbook-authoring skill).",
            })
        if not code:
            return json.dumps({"error": "Provide 'code' — the full playbook source."})
        pb_def, err = _compile_code(code, name=name)
        if err:
            return err
        stored_code: str | None = code

        defn = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
        defn["name"] = name

        async with session_factory() as session:
            existing = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if existing and existing.status != "archived":
                return json.dumps({"error": f"Playbook '{name}' already exists"})
            all_pb = await _load_all_playbook_steps(session, exclude=name)

            # The compiler already rejects unknown kwargs, and the dump
            # carries cross-kind defaults (fan_in/concurrency/...) the key
            # checker would falsely flag — so skip the unknown-key check.
            issues = validate_definition(
                defn,
                tool_registry=getattr(runner, "_tools", None), all_playbooks=all_pb,
                check_unknown_keys=False,
            )
            errors = [i.to_dict() for i in issues if i.severity == "error"]
            if errors:
                return json.dumps({
                    "error": "Playbook is invalid — fix these before it can be created.",
                    "issues": errors,
                })
            warnings = [i.to_dict() for i in issues if i.severity == "warning"]

            if existing:
                # plans/017: an archived playbook no longer squats its name —
                # the proposal takes over its row (id kept so run history
                # survives; the version counter keeps climbing so old runs
                # stay attributed to their versions).
                playbook = existing
                playbook.version = (existing.version or 1) + 1
                playbook.live_version = playbook.version
                playbook.candidate_version = None
                playbook.failures_acked_version = None
                playbook.display_name = display_name or pb_def.display_name or name
                playbook.description = description or pb_def.description
                playbook.when_to_use = when_to_use or pb_def.when_to_use
                playbook.inputs_schema = pb_def.inputs
                playbook.definition = defn
                playbook.code = stored_code
                playbook.manifest = manifest
                playbook.agent_autonomy = agent_autonomy
                playbook.created_by = "agent"
                playbook.status = "enabled"
            else:
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
            artifact_ref="playbook:{name}",
            description=(
                "Create a new playbook from its FULL source, written all at "
                "once. Pass `code` — the playbook language "
                "(restricted Python: playbook(...) header, then "
                "x = tool(...)/llm(...)/loop(...)/if_(...) steps; see the "
                "playbook-authoring skill). The code is parsed and compiled, "
                "never executed."
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
                        "description": "Full playbook code",
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
            modes=["planning", "building"],
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
    # Attach the probe only when the SDK knows the field (luna plans/038).
    try:
        from luna_sdk import ProbeDef  # noqa: PLC0415

        async def _db_probe() -> dict[str, Any]:
            from sqlalchemy import text
            try:
                async with session_factory() as session:
                    await session.execute(text("select 1"))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "failure_class": "resource_gone",
                        "detail": f"plugin database unreachable: {e}"}
            return {"ok": True, "detail": "plugin database reachable"}

        tools[-1][0].probe = ProbeDef(kind="resource_read", handler=_db_probe)
    except ImportError:
        pass  # older core: playbook_list is simply unprobeable

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
        if nested := _nested_run_refusal():
            return nested
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
                "playbook_publish to make it live."
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
            # NOT chat_only (0.31.1): muted ops wake turns need the run tools
            # (modes are the sole gate — the BUG #3 rule). The 006.707
            # nested-agent recursion this flag used to prevent is handled by
            # _nested_run_refusal() in the handler instead.
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
            elif run.status == "failed":
                # 012 phase 4: a failed run still recorded the REAL outputs
                # of every step that ran — steer the agent to pin them as
                # spec stubs before it starts fixing from memory.
                pb = await session.get(Playbook, run.playbook_id)
                if pb is not None:
                    payload["hint"] = (
                        "Failed — but every step that ran recorded its real "
                        "output above. Pin those shapes as spec stubs before "
                        "fixing: playbook_spec_from_run("
                        f"name='{pb.name}', run_id='{run_id}')."
                    )
            return json.dumps(payload)

    tools.append((
        ToolDef(
            name="playbook_status",
            modes=["planning", "building"],
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
            modes=["planning", "building"],
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

    # plans/018 phase 3: the remaining prompt_always tools carry a `why` —
    # optional, but FIRST in the schema so the legacy approval card leads
    # with plain language instead of raw arguments.
    _WHY_PROP = {
        "type": "string",
        "description": (
            "One or two plain sentences FOR THE OWNER: why this change is "
            "needed, in everyday language. Shown at the top of the approval "
            "card — always provide it."
        ),
    }

    # --- playbook_set_autonomy ---
    async def _set_autonomy(
        *, name: str, why: str = "",
        agent_autonomy: str = "", publish_autonomy: str = "",
        require_specs: bool | None = None, require_run: bool | None = None,
    ) -> str:
        if (not agent_autonomy and not publish_autonomy
                and require_specs is None and require_run is None):
            return json.dumps({
                "error": "Nothing to change — pass agent_autonomy, "
                         "publish_autonomy, require_specs and/or require_run.",
            })
        valid = {e.value for e in AgentAutonomy}
        if agent_autonomy and agent_autonomy not in valid:
            return json.dumps({"error": f"Invalid autonomy: {agent_autonomy}. Valid: {sorted(valid)}"})
        if publish_autonomy and publish_autonomy not in ("ask", "auto"):
            return json.dumps({
                "error": f"Invalid publish_autonomy: {publish_autonomy}. "
                         "Valid: ['ask', 'auto']",
            })

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            old = playbook.agent_autonomy
            old_publish = getattr(playbook, "publish_autonomy", "ask")
            if agent_autonomy:
                playbook.agent_autonomy = agent_autonomy
            if publish_autonomy:
                playbook.publish_autonomy = publish_autonomy
            # plans/016 phase 6: switchable publish gates (Settings → Publish)
            if require_specs is not None:
                playbook.publish_require_specs = require_specs
            if require_run is not None:
                playbook.publish_require_run = require_run
            await session.commit()
            req_specs, req_run = playbook.publish_require_specs, playbook.publish_require_run
        result: dict[str, Any] = {
            "playbook": name,
            "old_autonomy": old,
            "new_autonomy": agent_autonomy or old,
            "old_publish_autonomy": old_publish,
            "new_publish_autonomy": publish_autonomy or old_publish,
            "publish_require_specs": req_specs,
            "publish_require_run": req_run,
            "status": "updated",
        }
        if publish_autonomy == "auto":
            # luna 098 removed the ops modes that once honored 'auto'; the
            # publish gates + approval card decide, not this flag.
            result["note"] = (
                "publish_autonomy no longer changes publishing: every "
                "agent publish runs the machine gates and raises the "
                "owner's approval card."
            )
        return json.dumps(result)

    tools.append((
        ToolDef(
            name="playbook_set_autonomy",
            description=(
                "Change per-playbook autonomy. agent_autonomy = who may RUN "
                "it: 'agent_may_trigger' (agent runs freely), "
                "'agent_must_confirm' (agent must ask first), 'manual_only' "
                "(agent cannot run it at all). publish_autonomy is legacy "
                "and no longer changes publishing — every agent publish "
                "runs the machine gates and raises the owner's approval "
                "card. require_specs / require_run switch the "
                "publish gates (Settings → Publish): off = the gate is still "
                "run and reported but never refuses a publish. Lead with "
                "`why` — the owner reads it on the approval card."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "why": _WHY_PROP,
                    "name": {"type": "string", "description": "Playbook name"},
                    "agent_autonomy": {
                        "type": "string",
                        "enum": ["agent_may_trigger", "agent_must_confirm", "manual_only"],
                        "description": "The new run-autonomy level",
                    },
                    "publish_autonomy": {
                        "type": "string",
                        "enum": ["ask", "auto"],
                        "description": "The new publish-autonomy level",
                    },
                    "require_specs": {
                        "type": "boolean",
                        "description": "Pushing a version requires all tests green",
                    },
                    "require_run": {
                        "type": "boolean",
                        "description": "Pushing a version requires at least one successful run",
                    },
                },
                "required": ["name"],
            },
            policy="prompt_always",
            risk_level="medium",
        ),
        _set_autonomy,
    ))

    # --- playbook_ack_failures ---
    # plans/014: dismisses the failing-playbooks prompt digest for the
    # CURRENT live version only. A later edit+publish re-arms the digest by
    # itself (the ack is version-scoped), so "ignore it" never silences a
    # playbook the owner has since changed.
    async def _ack_failures(*, name: str) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            live = playbook.live_version or playbook.version
            playbook.failures_acked_version = live
            await session.commit()
        return json.dumps({
            "playbook": name,
            "acked_version": live,
            "status": "acked",
            "note": (
                "Failure digest dismissed for this version. It re-appears "
                "only if the playbook changes and the new version fails."
            ),
        })

    tools.append((
        ToolDef(
            name="playbook_ack_failures",
            description=(
                "Dismiss the 'playbook failures needing your attention' notice "
                "for one playbook. Call this ONLY after the owner has decided "
                "what to do about the failures (ignore / fix later). Fixing the "
                "playbook (edit + publish) clears the notice by itself — no ack "
                "needed then."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                },
                "required": ["name"],
            },
            policy="auto_approve",
            risk_level="low",
        ),
        _ack_failures,
    ))

    # --- Whole-source authoring helpers + tools ---

    # (0.38.0) The legacy _snapshot_version helper lived here. It inserted a
    # row at the CURRENT counter — duplicating the number when a row already
    # existed — and had no callers left. Minting goes through
    # versioning.mint_version; nothing snapshots in place.

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
        return await _tolerant_get_version_row_fn(session, playbook, n)

    async def _ensure_live_row(
        session: AsyncSession, playbook: Playbook,
    ) -> PlaybookVersion:
        """Guarantee a version row exists for the current live content."""
        return await ensure_live_row(session, playbook)

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
        # plans/022 P6: a row with NO manifest never NULLs the live manifest.
        if restore_manifest and row.manifest:
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
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})

            if format == "json":
                return json.dumps(playbook.definition, indent=2)
            try:
                return _derive_code(playbook)
            except Exception as e:  # noqa: BLE001 — legacy defs must stay readable
                return json.dumps({
                    "error": f"Could not render code for '{name}': {e}",
                    "hint": "retry with format='json'",
                })

    tools.append((
        ToolDef(
            name="playbook_get_definition",
            modes=["planning", "building"],
            description=(
                "Get a playbook's full source so you can edit it. Returns the "
                "playbook CODE (the Python-like playbook language) by default — "
                "edit it and pass it back via playbook_edit(code=...), or make a "
                "targeted change with playbook_edit(old=..., new=...). "
                "format='json' returns the raw JSON IR instead."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "format": {
                        "type": "string",
                        "enum": ["code", "json"],
                        "default": "code",
                    },
                },
                "required": ["name"],
            },
        ),
        _playbook_get_definition,
    ))

    # --- plans/022 P4: coding-agent-grade reads -------------------------
    # The agent reads a playbook's history the way a coding agent reads
    # files: every version's code, specs, manifest, and runs, plus diffs.
    # All read tools are planning+building (identify inherits planning since
    # core plan 100) — during the meltdown the agent diagnosed blind.

    async def _versions(*, name: str) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            rows = (await session.execute(
                select(PlaybookVersion)
                .where(PlaybookVersion.playbook_id == playbook.id)
                .order_by(PlaybookVersion.version)
            )).scalars().all()
            spec_counts: dict[int, int] = {}
            for v, in (await session.execute(
                select(PlaybookSpec.playbook_version).where(
                    PlaybookSpec.playbook_id == playbook.id,
                )
            )).all():
                spec_counts[v] = spec_counts.get(v, 0) + 1
            run_counts: dict[int, dict[str, int]] = {}
            for v, status in (await session.execute(
                select(PlaybookRun.playbook_version, PlaybookRun.status).where(
                    PlaybookRun.playbook_id == playbook.id,
                )
            )).all():
                c = run_counts.setdefault(v, {})
                c[status] = c.get(status, 0) + 1
            live_n = _live_version_of(playbook)
            return json.dumps({
                "playbook": name,
                "live_version": live_n,
                "candidate_version": playbook.candidate_version,
                "count": len(rows),
                "versions": [
                    {
                        "version": r.version,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "author": r.author,
                        "message": r.message,
                        "promoted_from": r.promoted_from,
                        "has_code": bool(r.code),
                        "has_manifest": bool(r.manifest),
                        "spec_count": spec_counts.get(r.version, 0),
                        "runs": run_counts.get(r.version, {}),
                        "live": r.version == live_n,
                        "candidate": r.version == playbook.candidate_version,
                    }
                    for r in rows
                ],
            })

    tools.append((
        ToolDef(
            name="playbook_versions",
            modes=["planning", "building"],
            description=(
                "List EVERY stored version of a playbook — like a file "
                "listing of its history: version number, when and by whom, "
                "commit message, lineage (promoted_from), whether it has "
                "code/manifest, its spec (test) count, run counts by "
                "status, and which is live / candidate. Read any of them "
                "with playbook_version_read; compare with "
                "playbook_version_diff."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                },
                "required": ["name"],
            },
            policy="auto_approve",
            risk_level="low",
        ),
        _versions,
    ))

    async def _version_read(
        *, name: str, version: int, include_specs: bool = True,
        include_runs: bool = True,
    ) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            row = await _get_version_row(session, playbook, version)
            if row is None:
                return json.dumps({
                    "error": f"'{name}' has no stored version {version}.",
                    "hint": "playbook_versions lists what exists.",
                })
            try:
                code = _version_code(row)
            except Exception as e:  # noqa: BLE001 — legacy defs stay readable
                code = f"# (code could not be rendered: {e})"
            out: dict[str, Any] = {
                "playbook": name,
                "version": row.version,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "author": row.author,
                "message": row.message,
                "promoted_from": row.promoted_from,
                "live": row.version == _live_version_of(playbook),
                "candidate": row.version == playbook.candidate_version,
                "code": code,
                "manifest": row.manifest,
                "definition": row.definition,
            }
            if include_specs:
                specs = (await session.execute(
                    select(PlaybookSpec).where(
                        PlaybookSpec.playbook_id == playbook.id,
                        PlaybookSpec.playbook_version == row.version,
                    ).order_by(PlaybookSpec.name)
                )).scalars().all()
                out["specs"] = [
                    {
                        "name": s.name,
                        "carried_from": (s.spec or {}).get("carried_from"),
                        "last_result": s.last_result,
                        "spec": s.spec,
                    }
                    for s in specs
                ]
            if include_runs:
                runs = (await session.execute(
                    select(PlaybookRun).where(
                        PlaybookRun.playbook_id == playbook.id,
                        PlaybookRun.playbook_version == row.version,
                    ).order_by(PlaybookRun.started_at.desc()).limit(10)
                )).scalars().all()
                out["recent_runs"] = [
                    {
                        "run_id": str(r.id),
                        "status": r.status,
                        "trigger": r.trigger,
                        "is_test": bool(r.is_test),
                        "started_at": r.started_at.isoformat() if r.started_at else None,
                    }
                    for r in runs
                ]
            return json.dumps(out)

    tools.append((
        ToolDef(
            name="playbook_version_read",
            modes=["planning", "building"],
            description=(
                "Full read of ANY stored playbook version — the equivalent "
                "of `cat` on an old file: its code, JSON definition, "
                "manifest, specs (tests, with carried-from provenance and "
                "last results), and its 10 most recent runs. Use "
                "playbook_runs for a run's full failure output."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "version": {"type": "integer", "description": "Version number to read"},
                    "include_specs": {"type": "boolean", "default": True},
                    "include_runs": {"type": "boolean", "default": True},
                },
                "required": ["name", "version"],
            },
            policy="auto_approve",
            risk_level="low",
        ),
        _version_read,
    ))

    async def _version_diff(
        *, name: str, from_version: int, to_version: int,
    ) -> str:
        import difflib

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            row_a = await _get_version_row(session, playbook, from_version)
            row_b = await _get_version_row(session, playbook, to_version)
            missing = [
                str(n) for n, r in ((from_version, row_a), (to_version, row_b))
                if r is None
            ]
            if missing:
                return json.dumps({
                    "error": (
                        f"'{name}' has no stored version "
                        f"{' or '.join(missing)}."
                    ),
                    "hint": "playbook_versions lists what exists.",
                })

            def _safe_code(row: PlaybookVersion) -> str:
                try:
                    return _version_code(row)
                except Exception as e:  # noqa: BLE001
                    return f"# (code could not be rendered: {e})"

            code_diff = "\n".join(difflib.unified_diff(
                _safe_code(row_a).splitlines(),
                _safe_code(row_b).splitlines(),
                fromfile=f"{name}@v{from_version}",
                tofile=f"{name}@v{to_version}",
                lineterm="",
            ))
            manifest_diff = "\n".join(difflib.unified_diff(
                (row_a.manifest or "").splitlines(),
                (row_b.manifest or "").splitlines(),
                fromfile=f"manifest@v{from_version}",
                tofile=f"manifest@v{to_version}",
                lineterm="",
            ))
            return json.dumps({
                "playbook": name,
                "from_version": from_version,
                "to_version": to_version,
                "code_diff": code_diff or "(identical)",
                "manifest_diff": manifest_diff or "(identical)",
            })

    tools.append((
        ToolDef(
            name="playbook_version_diff",
            modes=["planning", "building"],
            description=(
                "Unified diff of playbook code + manifest between any two "
                "stored versions — how a coding agent compares two "
                "revisions of a file."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "from_version": {"type": "integer"},
                    "to_version": {"type": "integer"},
                },
                "required": ["name", "from_version", "to_version"],
            },
            policy="auto_approve",
            risk_level="low",
        ),
        _version_diff,
    ))

    async def _runs_read(
        *, name: str, version: int | None = None, status: str = "",
        limit: int = 10,
    ) -> str:
        limit = max(1, min(int(limit), 50))
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            q = (
                select(PlaybookRun)
                .where(PlaybookRun.playbook_id == playbook.id)
                .order_by(PlaybookRun.started_at.desc())
                .limit(limit)
            )
            if version is not None:
                q = q.where(PlaybookRun.playbook_version == version)
            if status:
                q = q.where(PlaybookRun.status == status)
            runs = (await session.execute(q)).scalars().all()
            out_runs: list[dict[str, Any]] = []
            for r in runs:
                entry: dict[str, Any] = {
                    "run_id": str(r.id),
                    "version": r.playbook_version,
                    "status": r.status,
                    "trigger": r.trigger,
                    "is_test": bool(r.is_test),
                    "inputs": r.inputs,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                }
                if r.status == "failed":
                    # plans/022 P4: reading a failing run must be as good as
                    # reading a CI log — FULL error text, never truncated.
                    failed_steps = (await session.execute(
                        select(PlaybookStepRun).where(
                            PlaybookStepRun.run_id == r.id,
                            PlaybookStepRun.status == "failed",
                        ).order_by(PlaybookStepRun.started_at)
                    )).scalars().all()
                    entry["failures"] = [
                        {
                            "step_id": s.step_id,
                            "step_kind": s.step_kind,
                            "error": s.error,
                            "inputs": s.inputs,
                        }
                        for s in failed_steps
                    ]
                out_runs.append(entry)
            return json.dumps({
                "playbook": name,
                "count": len(out_runs),
                "filters": {"version": version, "status": status or None},
                "runs": out_runs,
                **({"note": "No runs match these filters."} if not out_runs else {}),
            })

    tools.append((
        ToolDef(
            name="playbook_runs",
            modes=["planning", "building"],
            description=(
                "List a playbook's runs, newest first — filter by version= "
                "and/or status= (running/done/failed/cancelled). Failed "
                "runs include every failed step's FULL error text and "
                "resolved inputs (read it like a CI log). Use "
                "playbook_status for one run's complete step-by-step trace."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "version": {"type": "integer", "description": "Only runs of this version"},
                    "status": {
                        "type": "string",
                        "enum": ["running", "done", "failed", "cancelled"],
                    },
                    "limit": {"type": "integer", "default": 10, "maximum": 50},
                },
                "required": ["name"],
            },
            policy="auto_approve",
            risk_level="low",
        ),
        _runs_read,
    ))

    # --- playbook_validate (the compiler) ---
    async def _validate(*, name: str = "", definition_yaml: str = "", code: str = "") -> str:
        # plans/023: YAML input removed — steering hint for stale callers.
        if definition_yaml:
            return json.dumps({
                "error": "YAML validation was removed — pass code= (full "
                         "playbook source) or name= (a saved playbook) instead.",
            })
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
                    "language_reference": LANGUAGE_CHEATSHEET,
                })
            defn = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
            # compiler already rejects unknown kwargs; the dump carries
            # cross-kind defaults the key checker would falsely flag.
            check_keys = False
        elif name:
            async with session_factory() as session:
                pb = (await session.execute(
                    select(Playbook).where(Playbook.name == name)
                )).scalar_one_or_none()
            if not pb:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            defn = pb.definition
        else:
            return json.dumps({"error": "Provide 'name' or 'code'."})

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
        result: dict[str, Any] = {
            "ok": not errors, "errors": errors, "warnings": warnings,
            "saved": False,
            "note": (
                "Validation only — NOTHING was saved. To persist a change, "
                "call playbook_edit (existing playbook) or playbook_propose "
                "(new playbook)."
            ),
        }
        # plans/003 phase 4: attach the spec on FAILED validation only — a
        # green result needs no recall, and the sheet is ~2KB per call.
        if errors:
            result["language_reference"] = LANGUAGE_CHEATSHEET
        return json.dumps(result)

    tools.append((
        ToolDef(
            name="playbook_validate",
            modes=["planning", "building"],
            description=(
                "Statically check a playbook WITHOUT running it (the compiler). "
                "Returns ALL issues at once: compile errors, schema errors, unknown "
                "keys, undefined {{inputs}}/{{steps}} references, use-before-define, "
                "bad loops, unknown tools, subtask cycles, and context-economy "
                "warnings. Pass a saved playbook 'name' or playbook 'code' "
                "(preferred). Run this before saving or running."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Saved playbook name"},
                    "code": {"type": "string", "description": "Full playbook code to check"},
                },
            },
            policy="auto_approve",
            risk_level="low",
        ),
        _validate,
    ))

    # --- playbook_language_reference (plans/003 phase 4: on-demand recall) ---
    async def _language_reference() -> str:
        return json.dumps({"language_reference": LANGUAGE_CHEATSHEET})

    tools.append((
        ToolDef(
            name="playbook_language_reference",
            modes=["planning", "building"],
            description=(
                "The complete playbook-language quick reference: every "
                "combinator with its exact kwargs, value assignment "
                "(x = expr), state ops, reference shapes "
                "(steps/vars/inputs paths), and the full Jinja filter list. "
                "Call this instead of guessing syntax — one wrong guess "
                "costs a whole edit/validate cycle."
            ),
            parameters={"type": "object", "properties": {}},
            policy="auto_approve",
            risk_level="low",
        ),
        _language_reference,
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
    # the current source before saving. 021: the manifest is CONTEXT, not
    # law — the drift gate (LLM judge + playbook_edit_force) was removed.

    async def _edit_impl(
        *,
        name: str,
        ticket: str = "",
        code: str = "",
        old: str = "",
        new: str = "",
        definition_yaml: str = "",
    ) -> str:
        # 0.14.0 (plans/002 phase 7): YAML input removed — steering hint for
        # stale callers instead of a TypeError.
        if definition_yaml:
            return json.dumps({
                "error": "YAML editing was removed — pass code= (full source) "
                         "or old=/new= (targeted snippet) instead.",
            })
        snippet_mode = bool(old) or bool(new)
        modes = sum([bool(code), snippet_mode])

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
                header = {
                    "stage": "read",
                    "editing": "candidate" if cand_row else "live",
                    "version": playbook.version,
                    "live_version": _live_version_of(playbook),
                    "candidate_version": playbook.candidate_version,
                    "ticket": str(t.id),
                    "expires_in_seconds": _TICKET_TTL_SECONDS,
                    "instructions": (
                        "Below: the manifest and current code as plain text. "
                        "The manifest is the bigger picture — read it before "
                        "changing things; it is not enforced, and if it is "
                        "outdated, update it (playbook_manifest_set). "
                        "Copy exact lines from the code block into old= for a "
                        "targeted edit. Then call playbook_edit again with "
                        "this ticket and exactly one of: code= (full source) "
                        "or old=/new= (targeted snippet). The ticket is "
                        "single-use and expires. Saving creates a CANDIDATE — "
                        "the live playbook keeps running unchanged until "
                        "playbook_publish."
                    ),
                }
                manifest_text = playbook.manifest
                if not manifest_text:
                    header["manifest_note"] = (
                        "This playbook has no manifest yet. Consider "
                        "proposing one to the owner via playbook_manifest_set."
                    )
                await session.commit()
            # 012 phase 2: code with real newlines, not a JSON-escaped
            # one-liner — the agent quotes old= snippets straight from it.
            code_label = (
                f"candidate v{header['candidate_version']}" if cand_row
                else f"live v{header['live_version']}"
            )
            # Frames round-trip exactly: each marker owns its leading "\n",
            # so a section ending in "\n" keeps it when parsed back out.
            return (
                json.dumps(header)
                + "\n--- manifest ---\n"
                + (manifest_text or "(none)")
                + f"\n--- code ({code_label}) ---\n"
                + current
                # 012 phase 3: the mini-reference rides on every edit; the
                # full sheet stays one call away (playbook_language_reference)
                # and still attaches to failed validate/compile results.
                + "\n--- language reference ---\n"
                + LANGUAGE_MINIREF
                + "\n--- end ---"
            )

        if modes != 1:
            return json.dumps({
                "error": "Provide exactly one of: 'code' or 'old'+'new'.",
            })
        if snippet_mode and not (old and new is not None):
            return json.dumps({"error": "Snippet edits need both 'old' and 'new'."})

        # WRITE stage, part 1: ticket check + compile + validate.
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

            pb_def, err = _compile_code(code, name=name)
            if err:
                return err
            stored_code = code
            check_target: Any = pb_def.model_dump(
                mode="json", exclude_none=True, by_alias=True,
            )

            all_pb = await _load_all_playbook_steps(session, exclude=name)
            issues = validate_definition(
                check_target,
                tool_registry=getattr(runner, "_tools", None), all_playbooks=all_pb,
                # compiled dumps carry cross-kind defaults the key checker
                # would falsely flag; the compiler already rejects typos.
                check_unknown_keys=False,
            )
            errors = [i.to_dict() for i in issues if i.severity == "error"]
            if errors:
                return json.dumps({
                    "error": "Edit rejected — the new definition is invalid.",
                    "issues": errors,
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
            # plans/016 phase 5: the new candidate inherits the specs of
            # the version it was edited from (previous candidate, else live).
            cand_row = await mint_version(
                session, playbook,
                definition=data, code=stored_code, manifest=playbook.manifest,
                author="agent",
                message="candidate",
                source_version=spec_source_version(playbook),
            )
            playbook.candidate_version = playbook.version
            await session.commit()
            new_version = playbook.version
            live_version = _live_version_of(playbook)
            # 0.11.0: auto-run the playbook's specs against the fresh
            # candidate (dry-run — cheap, no side effects). A failing spec
            # does NOT block the save; it blocks PROMOTE.
            spec_summary = await run_all_specs(
                session, runner, playbook.id,
                _shim_playbook(playbook, cand_row), new_version,
            )
            await session.commit()  # persist last_result caches

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
                "playbook_publish(name) to make it live. playbook_rollback "
                "restores the previous live version after a publish."
            ),
        }
        if spec_summary["total"]:
            result["specs"] = {
                "passed": spec_summary["passed"],
                "failed": spec_summary["failed"],
            }
            if spec_summary["failed"]:
                result["specs"]["failures"] = [
                    r for r in spec_summary["results"] if not r["passed"]
                ]
                result["next"] = (
                    f"{spec_summary['failed']} spec(s) FAILED against this "
                    "candidate — playbook_publish will refuse until they "
                    "pass. Fix the code (playbook_edit) or update the spec "
                    "(playbook_spec_add upserts by name) if the expectation "
                    "itself changed."
                )
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
    }

    tools.append((
        ToolDef(
            name="playbook_edit",
            artifact_ref="playbook:{name}",
            # 0.6.0 (luna 074/phase4): no longer chat_only. Headless turns
            # (scheduled fires, playbook agent_steps) could only reach
            # playbook_validate — the no-op twin with a near-identical schema
            # — so scheduled "update the playbook" tasks silently saved
            # nothing. Headless tool calls go through the same dispatch/
            # approval gate as chat since luna 0.40.003, and the edit
            # validates + snapshots a version before replacing.
            description=(
                "Change an existing playbook — a two-step flow. STEP 1 (read): "
                "call with ONLY the name; you get the playbook's manifest (the "
                "bigger picture — read it before changing things; update it "
                "via playbook_manifest_set when it's outdated), the current "
                "code as plain readable text "
                "(copy old= snippets from it verbatim), and a single-use edit "
                "ticket. STEP 2 (write): call again with that ticket plus "
                "exactly one of code= (full new source) or old=/new= (targeted "
                "snippet; 'old' must match exactly one place). "
                "The write compiles, validates, snapshots a version, "
                "then saves a CANDIDATE — live keeps running until "
                "playbook_publish."
            ),
            parameters={
                "type": "object",
                "properties": _EDIT_PAYLOAD_PROPS,
                "required": ["name"],
            },
        ),
        _playbook_edit,
    ))

    # --- playbook_manifest_set ---
    async def _manifest_set(*, name: str, manifest: str, why: str = "") -> str:
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
            old_live = _live_version_of(playbook)
            playbook.manifest = manifest
            await mint_version(
                session, playbook,
                definition=playbook.definition, code=playbook.code,
                manifest=manifest, author="agent",
                message="manifest updated" + (f": {why}" if why else ""),
                source_version=old_live,
            )
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
            artifact_ref="playbook:{name}",
            description=(
                "Set or replace a playbook's MANIFEST — the bigger picture "
                "in plain markdown: Purpose, Side effects, Never "
                "(invariants), Acceptance. It is context, not law: nothing "
                "enforces it, but it helps anyone editing see the whole "
                "before changing a part. Keep it short and true — update it "
                "whenever the playbook's intent drifts from what it says."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "why": _WHY_PROP,
                    "name": {"type": "string", "description": "Playbook name"},
                    "manifest": {
                        "type": "string",
                        "description": "Full manifest text (markdown). Replaces the current one.",
                    },
                },
                "required": ["name", "manifest"],
            },
            policy="auto_approve",
            risk_level="low",
        ),
        _manifest_set,
    ))

    _ALL_MODES = ["planning", "building"]  # every state that exists (luna 098)

    # --- playbook_publish (the gate: nothing goes live except through here) ---
    # 0.10.0 (plans/002 phase 3): promotion runs an extensible gate list.
    # A refusal names the failing gate.

    async def _request_publish_decision(
        *,
        name: str,
        action: str,
        target_version: int,
        explanation: str,
        evidence: Any,
        failed_run: Any = None,
        gates: list[dict[str, Any]],
        before_code: str,
        after_code: str,
        manifest_before: str,
        manifest_after: str,
        specs_added: list[str],
        specs_removed: list[str],
    ) -> str | None:
        """plans/018 phase 1: ONE owner approval for the whole change, raised
        AFTER every gate passed — owner-language presentation (luna 094) up
        front, the technical diff collapsed behind it. Returns a refusal JSON
        when the owner rejected, None to proceed. Contexts without an
        approval engine (unit tests, headless cores) proceed ungated — the
        old prompt_always card did not exist there either.

        021: the card carries ✓/✗ status bullets built from the gates list,
        so the owner sees the picture (tests green? test run done?) before
        deciding. Every agent publish raises the card — no standing skip.
        """
        # plans/022 P2: approvals fail CLOSED. Only a truly headless context
        # (no ctx at all — unit tests, headless cores) proceeds ungated; a
        # live context whose approval engine is broken/unwired ABORTS —
        # "approval infrastructure failed" must never read as "approved".
        if ctx is None:
            _log.warning(
                "publish proceeding without owner approval (headless, no "
                "ctx) playbook=%s action=%s", name, action,
            )
            return None
        try:
            approvals = ctx.approval
        except Exception:  # noqa: BLE001
            approvals = None
        if approvals is None:
            return json.dumps({
                "error": (
                    "Approval not obtained — nothing was published. The "
                    "approval engine is unavailable in this context."
                ),
                "hint": (
                    "Retry when the approval system is back, or the owner "
                    "can publish from the playbook page."
                ),
            })

        verb = "Restore" if action == "rollback" else "Publish"
        headline = (
            explanation.splitlines()[0].strip()[:90] if explanation
            else f"{verb} version {target_version} of '{name}'"
        )
        # plans/022 P1: the evidence line states the REAL run status — "green"
        # only when a run actually passed.
        if evidence is not None:
            evidence_line = (
                f"Evidence: green run of version {target_version} "
                f"(run {evidence.id})."
            )
        elif failed_run is not None:
            evidence_line = (
                f"Evidence: NONE — the latest run of version "
                f"{target_version} FAILED (run {failed_run.id}). The run "
                "gate is not enforced (Settings → Publish)."
            )
        else:
            evidence_line = (
                f"Evidence: none — no completed run of version "
                f"{target_version}."
            )
        changes: list[dict[str, Any]] = []
        # 021: ✓/✗ status bullets — the gate picture in owner words.
        check_lines = [
            ("✓ " if g["ok"] else "✗ ") + _gate_owner_line(g)
            for g in gates
        ]
        if check_lines:
            changes.append({
                "label": "Checks", "kind": "text",
                "text": "\n".join(check_lines),
            })
        if before_code != after_code:
            changes.append({
                "label": "Playbook code", "kind": "diff",
                "before": before_code, "after": after_code,
            })
        if manifest_before != manifest_after:
            changes.append({
                "label": "Manifest", "kind": "diff",
                "before": manifest_before, "after": manifest_after,
            })
        if specs_added or specs_removed:
            spec_lines = []
            if specs_added:
                spec_lines.append("Specs added: " + ", ".join(specs_added))
            if specs_removed:
                spec_lines.append("Specs removed: " + ", ".join(specs_removed))
            changes.append({
                "label": "Specs", "kind": "text",
                "text": "\n".join(spec_lines),
            })
        presentation = {
            "eyebrow": "Playbook change",
            "headline": headline,
            "explanation": f"{explanation}\n\n{evidence_line}",
            "changes": changes,
        }
        # payload identity drives dedup/supersede/grants — presentation is
        # advisory and must never leak into it.
        payload = {"name": name, "version": target_version, "action": action}
        summary = (
            f"{verb} playbook '{name}' version {target_version}: {headline}"
        )
        ops = await ops_conversation_id(ctx)
        try:
            decision = await approvals.request(
                kind="playbook_change",
                summary=summary,
                payload=payload,
                requested_by_plugin="plugin-playbooks",
                risk_level="medium",
                conversation_id=ops,
                presentation=presentation,
            )
        except Exception as e:  # noqa: BLE001 — plans/022 P2: fail CLOSED
            _log.exception(
                "publish approval wait failed playbook=%s action=%s",
                name, action,
            )
            return json.dumps({
                "error": (
                    "Approval not obtained — nothing was published. The "
                    f"approval wait failed ({type(e).__name__}); the owner "
                    "never decided."
                ),
                "hint": (
                    "Do NOT retry in a loop. Tell the owner an approval "
                    "card may be pending, and retry the publish once they "
                    "confirm the card is gone."
                ),
            })
        if getattr(decision, "decision", None) == "approved":
            return None
        return json.dumps({
            "error": f"The owner did not approve this {action}.",
            "owner_reason": getattr(decision, "reason", None),
            "hint": (
                "Relay the owner's reason in your reply and stand down — do "
                "not retry the publish unless the owner asks for it."
            ),
        })
    async def _do_publish(
        name: str, version: int | None, *, action: str, explanation: str = "",
    ) -> str:
        """0.26.0 (plans/015, 089 contract #8): THE publish function — the
        only way any version becomes live. version=None publishes the
        candidate through the full gate; version=N restores a previously
        stored version (rollback = publishing an older version through this
        same function). Preconditions are machine-checked HERE, never LLM
        discretion, and every success is announced in the ops chat.

        0.30.0 (plans/018 phase 1): after every gate passes, the handler
        raises ONE owner approval for the whole change — plain-language
        `explanation` up front, code/manifest diff collapsed behind it — and
        only flips live once the owner approves. The gates run under the row
        lock; the wait does not (an owner decision can take hours), so the
        flip re-checks the approved target is still current."""
        explanation = (explanation or "").strip()
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            is_candidate = version is None
            if is_candidate:
                if not playbook.candidate_version:
                    return json.dumps({
                        "error": f"'{name}' has no candidate to publish. Save "
                                 "an edit first (playbook_edit).",
                    })
                row = await _get_version_row(
                    session, playbook, playbook.candidate_version,
                )
                if row is None:
                    return json.dumps({
                        "error": "Candidate version row is missing (corrupt "
                                 "state) — save the edit again.",
                    })
            else:
                if version == _live_version_of(playbook):
                    return json.dumps({
                        "error": f"Version {version} is already live.",
                    })
                row = await _get_version_row(session, playbook, version)
                if row is None:
                    return json.dumps({
                        "error": f"No stored content for version {version} — "
                                 "cannot publish it.",
                    })

            gates: list[dict[str, Any]] = []
            # gate 1: static validation of the definition going live.
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
                    "error": "Publish refused — gate 'static_validation' failed.",
                    "gate": "static_validation",
                    "issues": errors,
                    "hint": "Fix the candidate via playbook_edit and retry.",
                })
            # gate 2 (0.11.0): specs. plans/016 phase 5: specs belong to a
            # version, so a restore runs the RESTORED version's own specs
            # against its content — the gate applies to candidates AND
            # restores (supersedes the plans/015 deviation that skipped it).
            spec_gate, spec_refusal = await specs_gate(
                session, runner, playbook.id,
                _shim_playbook(playbook, row), row.version,
                require=playbook.publish_require_specs,
            )
            gates.append(spec_gate)
            if spec_refusal is not None:
                await session.commit()  # persist last_result on the spec rows
                return json.dumps(spec_refusal)
            # gate 3 (0.26.0, 089 contract #8): the TEST-RUN gate — a green
            # run of this EXACT version recorded after the version row was
            # created (rows are immutable, so that is "since its last edit").
            # For restores the version's live history counts as evidence.
            test_gate, refusal, evidence, failed_run = await test_run_gate(
                session, playbook.id, row.version, row.created_at,
                include_live=not is_candidate,
                require=playbook.publish_require_run,
            )
            gates.append(test_gate)
            if refusal:
                return refusal
            # capture before commit — expired ORM attrs must not be touched
            # after the session closes.
            evidence_ref = SimpleNamespace(
                id=evidence.id, completed_at=evidence.completed_at,
            ) if evidence is not None else None
            failed_run_ref = SimpleNamespace(
                id=failed_run.id, completed_at=failed_run.completed_at,
            ) if failed_run is not None else None
            # gate 4 (0.12.0): probes — every tool the version touches must
            # not be KNOWN-broken. Only `failed` probes block; `unprobeable`
            # (no probe declared) passes with a note. Results are cached on
            # playbook_probe_results (committed even on refusal).
            probe_summary = await run_preflight(
                session, runner._tools, playbook, row.definition or {},
            )
            gates.append({
                "gate": "probes",
                "ok": probe_summary["failed"] == 0,
                "note": preflight_note(probe_summary),
            })
            if probe_summary["failed"]:
                await session.commit()  # persist the probe cache rows
                return json.dumps({
                    "error": "Publish refused — gate 'probes' failed.",
                    "gate": "probes",
                    "failing_tools": [
                        r for r in probe_summary["results"]
                        if r["status"] == "failed"
                    ],
                    "hint": (
                        "A tool this playbook uses is broken or missing "
                        "(dead credential, removed plugin, blocked policy). "
                        "Fix the connection/plugin, or edit the playbook to "
                        "stop using the tool, then publish again."
                    ),
                })

            # plans/018 phase 1: gather what the approval card and the later
            # flip need, then release the row lock — the owner decision waits
            # outside any transaction. Spec/probe caches commit here.
            old_live = _live_version_of(playbook)
            target_version = row.version
            playbook_name = playbook.name
            try:
                before_code = _derive_code(playbook)
            except Exception:  # noqa: BLE001 — legacy defs may not codegen
                before_code = playbook.code or ""
            try:
                after_code = _version_code(row)
            except Exception:  # noqa: BLE001
                after_code = row.code or ""
            manifest_before = playbook.manifest or ""
            manifest_after = (
                manifest_before if is_candidate else (row.manifest or "")
            )
            spec_names_before = set((await session.execute(
                select(PlaybookSpec.name).where(
                    PlaybookSpec.playbook_id == playbook.id,
                    PlaybookSpec.playbook_version == old_live,
                )
            )).scalars())
            spec_names_after = set((await session.execute(
                select(PlaybookSpec.name).where(
                    PlaybookSpec.playbook_id == playbook.id,
                    PlaybookSpec.playbook_version == target_version,
                )
            )).scalars())
            await session.commit()

        refusal = await _request_publish_decision(
            name=playbook_name, action=action,
            target_version=target_version, explanation=explanation,
            evidence=evidence_ref, failed_run=failed_run_ref, gates=gates,
            before_code=before_code, after_code=after_code,
            manifest_before=manifest_before, manifest_after=manifest_after,
            specs_added=sorted(spec_names_after - spec_names_before),
            specs_removed=sorted(spec_names_before - spec_names_after),
        )
        if refusal is not None:
            return refusal

        # Re-lock and flip: the approved target must still be current.
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            if is_candidate:
                if playbook.candidate_version != target_version:
                    return json.dumps({
                        "error": (
                            "The candidate changed while the approval was "
                            f"pending — the owner approved version "
                            f"{target_version}, but the candidate is now "
                            f"{playbook.candidate_version}. Review the new "
                            "candidate, test it, and publish again."
                        ),
                    })
            elif target_version == _live_version_of(playbook):
                return json.dumps({
                    "error": f"Version {target_version} is already live.",
                })
            row = await _get_version_row(session, playbook, target_version)
            if row is None:
                return json.dumps({
                    "error": (
                        f"No stored content for version {target_version} — "
                        "cannot publish it."
                    ),
                })
            old_live = _live_version_of(playbook)
            await _ensure_live_row(session, playbook)
            # candidate publish keeps the live manifest (drift was checked at
            # save); a restore brings the old manifest back with the content.
            _apply_version_to_live(
                playbook, row, restore_manifest=not is_candidate,
            )
            if is_candidate:
                row.promoted_from = old_live  # rollback lineage
                playbook.candidate_version = None
            new_live = playbook.live_version
            change_summary = (row.message or "") if is_candidate else ""
            await session.commit()

        # live content changed — resync triggers and refresh the canvas.
        await events.emit("playbook.saved", {"name": name})
        await events.emit("ui.plugin.event", {
            "plugin": "plugin-playbooks",
            "event": "playbook.patch",
            "payload": {"draft_id": name, "action": "replace", "name": name},
            "focus": True,
        })
        # contract #8: announce in the ops chat (version + test evidence)
        # and emit `playbook.published` for other plugins/UI.
        await announce_publish(
            ctx, events,
            name=name,
            old_version=old_live,
            new_version=new_live,
            evidence=evidence_ref,
            actor="agent",
            action=action,
            summary=change_summary,
            failed_run=failed_run_ref,
        )
        rolled_back = action == "rollback"
        # plans/022 P1: machine-readable evidence truth for downstream chats
        # and the ops inbox.
        spec_gate_entry = next(
            (g for g in gates if g.get("gate") == "specs"), {},
        )
        evidence_block = {
            "run_id": (
                str(evidence_ref.id) if evidence_ref is not None
                else str(failed_run_ref.id) if failed_run_ref is not None
                else None
            ),
            "status": (
                "passed" if evidence_ref is not None
                else "failed" if failed_run_ref is not None
                else "none"
            ),
            "spec_count": spec_gate_entry.get("total", 0),
            "specs_passed": spec_gate_entry.get("passed", 0),
        }
        return json.dumps({
            "playbook": name,
            "status": "rolled_back" if rolled_back else "published",
            "live_version": new_live,
            "previous_live_version": old_live,
            "gates": gates,
            "evidence": evidence_block,
            "note": (
                f"Version {new_live} is live again (manifest included). "
                f"Version {old_live} stays in history — publish a new "
                "candidate to move forward."
                if rolled_back else
                f"Version {new_live} is now LIVE — triggers and "
                f"playbook_run execute it. playbook_rollback(name) "
                f"restores version {old_live} if it misbehaves."
            ),
        })

    async def _publish(
        *, name: str, explanation: str = "", version: int | None = None,
    ) -> str:
        return await _do_publish(
            name, version, action="publish", explanation=explanation,
        )

    tools.append((
        ToolDef(
            name="playbook_publish",
            artifact_ref="playbook:{name}",
            artifact_verb="publishing",
            description=(
                "Publish a playbook version — the ONLY way content goes "
                "live. Default: publishes the CANDIDATE. version=N restores "
                "a previously stored version instead. The gate is "
                "machine-checked and refuses with the exact reason: static "
                "validation, specs, a GREEN TEST RUN of that exact version "
                "since its last edit (playbook_run_candidate provides it — "
                "run the test BEFORE publishing), and tool probes. After "
                "the gates pass, the owner gets ONE approval card for the "
                "whole change: ✓/✗ check bullets and your `explanation` in "
                "plain language up front, the technical diff collapsed "
                "behind it. Every publish is announced in the ops chat "
                "with its evidence."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "explanation": {
                        "type": "string",
                        "description": (
                            "Optional, 2-6 sentences addressed to the OWNER "
                            "in plain language: what issue was found, what "
                            "this version changes, why it is safe to go "
                            "live. Shown on the approval card — no jargon, "
                            "no stack traces."
                        ),
                    },
                    "version": {
                        "type": "integer",
                        "description": (
                            "Publish this stored version instead of the "
                            "candidate (restore an older version)."
                        ),
                    },
                },
                "required": ["name"],
            },
            # plans/018 phase 1: the owner approval is raised by the handler
            # AFTER the gates pass (one rich card per change) — the core
            # gate's per-call prompt would be a second, redundant ask.
            policy="auto_approve",
            # 0.30.3: the handler PARKS on the owner's approval card — the
            # default 30s tool timeout killed every publish the owner didn't
            # answer within half a minute (wait_for cancels the handler, so
            # a late approval resumed nothing and the card was orphaned).
            timeout_seconds=900,
            risk_level="medium",
            modes=_ALL_MODES,
        ),
        _publish,
    ))

    # --- playbook_rollback (live ← previous live, via the publish path) ---
    async def _rollback(*, name: str, explanation: str = "") -> str:
        # 0.26.0 (plans/015, 089 contract #8): rollback resolves the target
        # version, then publishes it through the SAME gated function.
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
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
        return await _do_publish(
            name, target_n, action="rollback", explanation=explanation,
        )

    tools.append((
        ToolDef(
            name="playbook_rollback",
            artifact_ref="playbook:{name}",
            artifact_verb="publishing",
            description=(
                "Restore a playbook's PREVIOUS live version (the one the "
                "current live was published from). Use when a published "
                "change misbehaves. Runs through the same publish gate "
                "(the prior version's live history is its test evidence), "
                "asks the owner with ONE plain-language approval card, and "
                "announces in the ops chat."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "explanation": {
                        "type": "string",
                        "description": (
                            "Optional, 2-6 sentences addressed to the OWNER "
                            "in plain language: what went wrong with the "
                            "current version, why rolling back is the right "
                            "fix. Shown on the approval card — no jargon, "
                            "no stack traces."
                        ),
                    },
                },
                "required": ["name"],
            },
            # plans/018 phase 1: owner approval raised handler-side (see
            # playbook_publish).
            policy="auto_approve",
            # 0.30.3: parks on the owner card, same as playbook_publish.
            timeout_seconds=900,
            risk_level="medium",
            modes=_ALL_MODES,
        ),
        _rollback,
    ))

    # --- playbook_run_candidate (supervised real test run) ---
    async def _run_candidate(
        *, name: str, inputs: str = "{}", wait_seconds: float | None = None,
    ) -> str:
        if nested := _nested_run_refusal():
            return nested
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

        # 0.26.0 (plans/015, 089): candidate runs ARE the test evidence the
        # publish gate looks for — stamped is_test at creation.
        run = await runner.start_run_background(
            shim, inputs=input_data, trigger="agent-candidate", is_test=True,
        )
        waited = await runner.wait_for_run(run.id, timeout=wait_seconds)
        status = waited.status if waited else run.status

        result: dict[str, Any] = {
            "run_id": str(run.id),
            "playbook": name,
            "candidate_version": candidate_version,
            "status": status,
            "note": (
                "This was a REAL test run of the CANDIDATE (side effects "
                "included). The live playbook is unchanged — call "
                "playbook_publish when it completes green."
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
                "Candidate test run FAILED. Do NOT fabricate results — "
                "playbook_publish will refuse until a green test run "
                "exists. Check playbook_status for the error details."
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
            artifact_ref="playbook:{name}",
            artifact_verb="testing",
            timeout_seconds=120,
            description=(
                "REAL, supervised test run of a playbook's CANDIDATE version "
                "— actual tools, actual side effects, recorded in run "
                "history against the candidate version number. The live "
                "playbook stays untouched. Prefer playbook_dry_run first; "
                "use this when the owner wants proof against real systems "
                "before playbook_publish."
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

    # --- specs: playbook tests (0.11.0, plans/002 phase 4) ---

    async def _spec_target(
        session: AsyncSession, playbook: Playbook, version: str,
    ) -> tuple[Any, int] | str:
        """Resolve which content specs run against: 'auto' = candidate when
        one exists else live; or 'candidate' / 'live' / a version number.
        Returns (target, version_n) or an error string."""
        v = (version or "auto").strip().lower()
        if v == "auto":
            v = "candidate" if playbook.candidate_version else "live"
        if v == "live":
            return playbook, _live_version_of(playbook)
        if v == "candidate":
            if not playbook.candidate_version:
                return f"'{playbook.name}' has no candidate version."
            row = await _get_version_row(
                session, playbook, playbook.candidate_version,
            )
            if row is None:
                return "Candidate version row is missing — save the edit again."
            return _shim_playbook(playbook, row), row.version
        try:
            n = int(v)
        except ValueError:
            return f"version must be 'auto', 'candidate', 'live', or a number — got '{version}'."
        if n == _live_version_of(playbook):
            return playbook, n
        row = await _get_version_row(session, playbook, n)
        if row is None:
            return f"No stored content for version {n}."
        return _shim_playbook(playbook, row), n

    async def _spec_add(
        *, name: str, spec_name: str = "", spec: dict | None = None,
        specs: dict | None = None, version: str = "auto",
        spec_yaml: str = "",
    ) -> str:
        # plans/012 phase 1: one call carries the whole suite. Two forms —
        # single (spec_name + spec) or batch (specs= object of spec-name →
        # spec body). Batch upserts everything, then runs the suite ONCE.
        # plans/023: spec_yaml is an undeclared kwarg kept only to steer
        # stale callers — YAML input was removed.
        if spec_yaml:
            return json.dumps({"error": (
                "YAML specs were removed — pass spec= (a JSON object) with "
                "spec_name, or specs= (JSON object of spec-name → spec body)."
            )})
        single = bool(spec_name or spec is not None)
        if single and specs:
            return json.dumps({"error": (
                "Provide either spec_name+spec (one spec) or specs= "
                "(batch) — not both."
            )})
        if single and not (spec_name and spec is not None):
            return json.dumps({"error": "A single spec needs both spec_name and spec."})
        parse_errors: dict[str, str] = {}
        if single:
            try:
                parsed = {spec_name: parse_spec(spec)}
            except ValueError as e:
                return json.dumps({"error": str(e)})
        else:
            if not specs:
                return json.dumps({"error": (
                    "Provide spec_name+spec (one spec) or specs= — a JSON "
                    "object of spec-name → spec body. Prefer specs=: "
                    "write ALL the specs you intend to add in ONE call."
                )})
            try:
                parsed, parse_errors = parse_spec_batch(specs)
            except ValueError as e:
                return json.dumps({"error": str(e)})
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            # plans/016 phase 5: specs are written to ONE version's set —
            # the candidate when one exists, else live (version= overrides).
            resolved = await _spec_target(session, playbook, version)
            if isinstance(resolved, str):
                return json.dumps({"error": resolved})
            target, version_n = resolved
            actions: dict[str, str] = {}
            for s_name, spec in parsed.items():
                row = (await session.execute(
                    select(PlaybookSpec).where(
                        PlaybookSpec.playbook_id == playbook.id,
                        PlaybookSpec.playbook_version == version_n,
                        PlaybookSpec.name == s_name,
                    )
                )).scalar_one_or_none()
                actions[s_name] = "updated" if row else "created"
                if row is None:
                    row = PlaybookSpec(
                        playbook_id=playbook.id, playbook_version=version_n,
                        name=s_name, created_by="agent",
                        spec=spec.model_dump(mode="json", exclude_none=True),
                    )
                    session.add(row)
                else:
                    row.spec = spec.model_dump(mode="json", exclude_none=True)
            if not parsed:
                return json.dumps({
                    "error": "No spec in the batch parsed — nothing stored.",
                    "spec_errors": parse_errors,
                })
            summary = await run_all_specs(
                session, runner, playbook.id, target, version_n,
                only_name=spec_name if single else None,
            )
            await session.commit()
        if single:
            res = summary["results"][0] if summary["results"] else None
            out: dict[str, Any] = {
                "playbook": name, "spec": spec_name, "status": actions[spec_name],
                "ran_against_version": version_n, "result": res,
            }
            if res and not res["passed"]:
                out["warning"] = (
                    "The spec FAILS against the current content — it was stored "
                    "anyway. playbook_publish will refuse while it fails."
                )
            return json.dumps(out)
        out = {
            "playbook": name, "specs": actions,
            "ran_against_version": version_n, **summary,
        }
        if parse_errors:
            out["spec_errors"] = parse_errors
            out["note"] = (
                f"{len(parse_errors)} spec(s) failed to parse and were NOT "
                "stored — fix and resend just those in one specs= call."
            )
        if summary.get("failed"):
            out["warning"] = (
                "Failing specs were stored anyway — playbook_publish will "
                "refuse while any spec fails."
            )
        return json.dumps(out)

    tools.append((
        ToolDef(
            name="playbook_spec_add",
            artifact_ref="playbook:{name}",
            description=(
                "Add or update (upsert by name) SPECS — stored tests for a "
                "playbook: fixture inputs, scripted stubs for "
                "tool/agent/llm steps, and assertions over the dry-run "
                "trace. PREFER BATCH: write ALL the specs you intend to add "
                "in ONE call via specs= (a JSON object of spec-name → spec "
                "body) — never one call per spec. Spec body keys: "
                "description, inputs {..}, stubs "
                "{step_id_or_tool_name: scripted_output}, expect {status: "
                "done|failed, steps_ran: [ids in order], steps_not_ran: "
                "[ids], tool_calls: {tool: {count, args_contain: {..}}}, "
                "output_contains: {step_id: substring}, error_contains}. "
                "Specs run immediately against the candidate (or live "
                "when none) and on every future candidate save; a failing "
                "spec blocks playbook_publish."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "specs": {
                        "type": "object",
                        "description": (
                            "BATCH (preferred): JSON object of spec-name → "
                            "spec body. All are upserted, then the whole "
                            "suite runs once."
                        ),
                    },
                    "spec_name": {"type": "string", "description": "Single form: spec name (unique per playbook version)"},
                    "spec": {"type": "object", "description": "Single form: the spec body (JSON object)"},
                    "version": {
                        "type": "string",
                        "description": "Which version's test set to write to: 'auto' (default: candidate when one exists, else live) | 'candidate' | 'live' | version number. Specs are duplicated to every new version.",
                    },
                },
                "required": ["name"],
            },
        ),
        _spec_add,
    ))

    async def _spec_list(*, name: str, version: str = "auto") -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            resolved = await _spec_target(session, playbook, version)
            if isinstance(resolved, str):
                return json.dumps({"error": resolved})
            _target, version_n = resolved
            rows = (await session.execute(
                select(PlaybookSpec)
                .where(
                    PlaybookSpec.playbook_id == playbook.id,
                    PlaybookSpec.playbook_version == version_n,
                )
                .order_by(PlaybookSpec.name)
            )).scalars().all()
        return json.dumps({
            "playbook": name,
            "version": version_n,
            "count": len(rows),
            "specs": [
                {
                    "name": r.name,
                    "description": (r.spec or {}).get("description", ""),
                    "carried_from": (r.spec or {}).get("carried_from"),
                    "created_by": r.created_by,
                    "last_result": r.last_result,
                    "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                    "last_version": r.last_version,
                }
                for r in rows
            ],
            **({"note": (
                "No specs — the playbook has no tests. "
                "playbook_spec_from_run pins a good real run as a spec; "
                "playbook_spec_add writes one from scratch."
            )} if not rows else {}),
        })

    tools.append((
        ToolDef(
            name="playbook_spec_list",
            modes=["planning", "building"],
            description=(
                "List one version's specs (its tests) with each spec's last "
                "result and when it last ran. Specs belong to a version."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "version": {
                        "type": "string",
                        "description": "'auto' (default: candidate when one exists, else live) | 'candidate' | 'live' | version number",
                    },
                },
                "required": ["name"],
            },
        ),
        _spec_list,
    ))

    async def _spec_delete(
        *, name: str, spec_name: str, version: str = "auto", why: str = "",
    ) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            resolved = await _spec_target(session, playbook, version)
            if isinstance(resolved, str):
                return json.dumps({"error": resolved})
            _target, version_n = resolved
            row = (await session.execute(
                select(PlaybookSpec).where(
                    PlaybookSpec.playbook_id == playbook.id,
                    PlaybookSpec.playbook_version == version_n,
                    PlaybookSpec.name == spec_name,
                )
            )).scalar_one_or_none()
            if row is None:
                return json.dumps({
                    "error": f"'{name}' v{version_n} has no spec named '{spec_name}'.",
                })
            # plans/022 P3: a CARRIED spec is inherited coverage — deleting
            # it needs a stated reason (visibility, not a gate: any reason
            # passes, but "silently vanished" is no longer possible).
            carried = (row.spec or {}).get("carried_from")
            if carried is not None and not why.strip():
                return json.dumps({
                    "error": (
                        f"Spec '{spec_name}' was carried forward from "
                        f"version {carried} — deleting inherited coverage "
                        "requires a reason. Pass why= (one sentence: why "
                        "this test no longer applies)."
                    ),
                })
            await session.delete(row)
            await session.commit()
        return json.dumps({
            "playbook": name, "version": version_n, "spec": spec_name, "status": "deleted",
            **({"carried_from": carried, "reason": why} if carried is not None else {}),
        })

    tools.append((
        ToolDef(
            name="playbook_spec_delete",
            artifact_ref="playbook:{name}",
            description=(
                "Delete one spec by name from one version's test set. This "
                "raises an approval card — lead with `why` so the owner "
                "understands what coverage is being dropped and why."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "why": _WHY_PROP,
                    "name": {"type": "string", "description": "Playbook name"},
                    "spec_name": {"type": "string", "description": "Spec to delete"},
                    "version": {
                        "type": "string",
                        "description": "'auto' (default: candidate when one exists, else live) | 'candidate' | 'live' | version number",
                    },
                },
                "required": ["name", "spec_name"],
            },
            policy="prompt_always",
            risk_level="medium",
        ),
        _spec_delete,
    ))

    async def _spec_run(
        *, name: str, spec_name: str = "", version: str = "auto",
    ) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            resolved = await _spec_target(session, playbook, version)
            if isinstance(resolved, str):
                return json.dumps({"error": resolved})
            target, version_n = resolved
            summary = await run_all_specs(
                session, runner, playbook.id, target, version_n,
                only_name=spec_name or None,
            )
            await session.commit()
        if spec_name and summary["total"] == 0:
            return json.dumps({
                "error": f"'{name}' has no spec named '{spec_name}'.",
            })
        is_cand = bool(playbook.candidate_version) and version_n == playbook.candidate_version
        return json.dumps({
            "playbook": name,
            "ran_against_version": version_n,
            "is_candidate": is_cand,
            **summary,
            **({"note": (
                "No specs defined — nothing was tested. This playbook has "
                "no safety net for publish."
            )} if summary["total"] == 0 else {}),
        })

    tools.append((
        ToolDef(
            name="playbook_spec_run",
            description=(
                "Run a playbook's specs (all, or one via spec_name=) as "
                "dry-runs with the spec's fixture inputs and stubs — no side "
                "effects. version= targets 'auto' (candidate when one "
                "exists, else live), 'candidate', 'live', or a number. "
                "Returns per-spec pass/fail with readable failure lines."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "spec_name": {"type": "string", "description": "Run just this spec"},
                    "version": {
                        "type": "string",
                        "description": "'auto' (default) | 'candidate' | 'live' | version number",
                    },
                },
                "required": ["name"],
            },
        ),
        _spec_run,
    ))

    async def _spec_from_run(*, name: str, run_id: str = "") -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            q = select(PlaybookRun).where(PlaybookRun.playbook_id == playbook.id)
            if run_id:
                try:
                    q = q.where(PlaybookRun.id == uuid.UUID(run_id))
                except ValueError:
                    return json.dumps({"error": f"'{run_id}' is not a run id"})
                run = (await session.execute(q)).scalars().first()
                if run is not None and run.status not in ("done", "failed"):
                    return json.dumps({
                        "error": f"Run {run_id} is still '{run.status}' — "
                                 "only finished runs (done or failed) can "
                                 "be pinned.",
                    })
            else:
                # 012 phase 4: prefer the latest done run, but fall back to
                # the latest failed one — even a failed run's recorded
                # outputs are the truth about real tool shapes.
                run = None
                for status in ("done", "failed"):
                    run = (await session.execute(
                        q.where(PlaybookRun.status == status).order_by(
                            PlaybookRun.started_at.desc()
                        ).limit(1)
                    )).scalars().first()
                    if run is not None:
                        break
            if run is None:
                return json.dumps({
                    "error": f"No finished run of '{name}' to pin"
                             + (f" (run {run_id} not found)" if run_id else "")
                             + ".",
                })
            steps = (await session.execute(
                select(PlaybookStepRun)
                .where(PlaybookStepRun.run_id == run.id)
                .order_by(PlaybookStepRun.started_at)
            )).scalars().all()
        doc = spec_from_run(run, steps, playbook.definition)
        return json.dumps({
            "playbook": name,
            "run_id": str(run.id),
            "run_version": run.playbook_version,
            "spec": doc,
            "next": (
                "This is a PROPOSAL built from the recorded run: trim stubs "
                "and expectations you don't care about (over-tight specs "
                "fail on harmless changes), give it a name, then save it "
                "with playbook_spec_add."
            ) if run.status == "done" else (
                "This is a PROPOSAL built from a FAILED run: the stubs pin "
                "the real outputs of every step that DID run — that part is "
                "the value. The expect block documents the current failure; "
                "after you fix the code, update expect to the good behavior "
                "and keep the stubs. Save with playbook_spec_add."
            ),
        })

    tools.append((
        ToolDef(
            name="playbook_spec_from_run",
            modes=["planning", "building"],
            description=(
                "Record & replay: build a spec PROPOSAL from a real "
                "finished run — recorded tool outputs become stubs, the "
                "run's inputs become fixture inputs, expectations are "
                "seeded from what the run actually did (status, step order, "
                "tool call counts). FAILED runs work too: stubs pin every "
                "step that DID run, expect documents the failure point. "
                "Defaults to the latest done run (falls back to the latest "
                "failed one); pass run_id= to pin a specific run. Returns "
                "a spec (JSON) to trim and save via playbook_spec_add — "
                "nothing is stored yet."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "run_id": {"type": "string", "description": "Specific run to pin (default: latest done)"},
                },
                "required": ["name"],
            },
        ),
        _spec_from_run,
    ))

    # --- playbook_preflight (0.12.0, plans/002 phase 5) ---
    async def _preflight(*, name: str, version: str = "auto") -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            resolved = await _spec_target(session, playbook, version)
            if isinstance(resolved, str):
                return json.dumps({"error": resolved})
            target, version_n = resolved
            summary = await run_preflight(
                session, runner._tools, playbook, target.definition or {},
            )
            await session.commit()
        result: dict[str, Any] = {
            "playbook": name,
            "checked_version": version_n,
            "is_candidate": bool(
                playbook.candidate_version
                and version_n == playbook.candidate_version
            ),
            **summary,
        }
        if summary["failed"]:
            broken = [r for r in summary["results"] if r["status"] == "failed"]
            result["next"] = (
                "BROKEN: " + "; ".join(
                    f"{r['tool']} ({r['failure_class']})" for r in broken
                ) + " — playbook_publish will refuse, and live runs would "
                "fail at these steps. Fix the connection/plugin or edit the "
                "playbook to stop using the tool."
            )
        elif summary["ok"] == 0:
            result["note"] = (
                "No tool declares a probe yet — nothing verified, nothing "
                "known-broken. This is normal today; probes arrive per-plugin."
            )
        return json.dumps(result)

    tools.append((
        ToolDef(
            name="playbook_preflight",
            modes=["planning", "building"],
            description=(
                "Check that every tool a playbook touches would work RIGHT "
                "NOW (credentials alive, resources reachable) — the check "
                "specs can't do because they stub the outside world. Probes "
                "each tool (including subtask targets' tools): ok / "
                "unprobeable (no probe declared) / failed. Failed probes "
                "block playbook_publish. version: auto (candidate when one "
                "exists, else live) | candidate | live | a number."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "version": {
                        "type": "string",
                        "description": "auto | candidate | live | version number",
                    },
                },
                "required": ["name"],
            },
        ),
        _preflight,
    ))

    return tools
