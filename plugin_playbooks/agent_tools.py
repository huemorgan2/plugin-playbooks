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
from .models import Playbook, PlaybookRun, PlaybookStepRun, PlaybookVersion
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
            author=author,
            message=message,
            promoted_from=promoted_from,
        )
        session.add(v)
        return v

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
    async def _dry_run(*, name: str, inputs: str = "{}") -> str:
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

        trace = await runner.dry_run(playbook, inputs=input_data)
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
                "SIMULATED — never report them to the user as real results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "inputs": {"type": "string", "description": "JSON string of inputs"},
                },
                "required": ["name"],
            },
        ),
        _dry_run,
    ))

    # --- playbook_edit (whole-source edit-in-place) ---
    async def _playbook_edit(
        *,
        name: str,
        code: str = "",
        old: str = "",
        new: str = "",
        definition_yaml: str = "",
    ) -> str:
        # 0.8.0 (plans/002 phase 1): three modes, exactly one —
        #   code=            full code replace (preferred)
        #   old= / new=      snippet diff applied to the current code
        #   definition_yaml= full YAML replace (legacy)
        snippet_mode = bool(old) or bool(new)
        modes = sum([bool(code), snippet_mode, bool(definition_yaml)])
        if modes != 1:
            return json.dumps({
                "error": "Provide exactly one of: 'code', 'old'+'new', or "
                         "'definition_yaml'.",
            })
        if snippet_mode and not (old and new is not None):
            return json.dumps({"error": "Snippet edits need both 'old' and 'new'."})

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({
                    "error": f"Playbook '{name}' not found. Use playbook_propose to create it.",
                })

            stored_code: str | None
            if snippet_mode:
                try:
                    current = _derive_code(playbook)
                except Exception as e:  # noqa: BLE001
                    return json.dumps({
                        "error": f"Cannot snippet-edit '{name}': its code "
                                 f"cannot be rendered ({e}). Use code= with "
                                 f"the full source instead.",
                    })
                count = current.count(old)
                if count == 0:
                    return json.dumps({
                        "error": "The 'old' snippet was not found in the "
                                 "current code. Call playbook_get_definition "
                                 "and copy the exact text.",
                    })
                if count > 1:
                    return json.dumps({
                        "error": f"The 'old' snippet matches {count} places — "
                                 "include more surrounding context so it is "
                                 "unique.",
                    })
                code = current.replace(old, new)

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

            await _snapshot_version(
                session, playbook, author="agent", message="before edit",
            )
            data = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
            data["name"] = name  # never rename via edit
            playbook.definition = data
            playbook.code = stored_code
            playbook.version += 1
            playbook.description = pb_def.description or playbook.description
            playbook.when_to_use = pb_def.when_to_use or playbook.when_to_use
            playbook.display_name = pb_def.display_name or playbook.display_name
            playbook.inputs_schema = pb_def.inputs
            await session.commit()
            new_version = playbook.version

        # resync triggers/bindings + refresh the open canvas.
        await events.emit("playbook.saved", {"name": name})
        # 009.001/phase04: auto-follow the change — the iframe maps
        # playbook.patch to open+patch, and focus brings the section up.
        await events.emit("ui.plugin.event", {
            "plugin": "plugin-playbooks",
            "event": "playbook.patch",
            "payload": {"draft_id": name, "action": "replace", "name": name},
            "focus": True,
        })
        warnings = [i.to_dict() for i in issues if i.severity == "warning"]
        return json.dumps({
            "playbook": name, "version": new_version, "status": "edited",
            "warnings": warnings,
        })

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
                "Change an existing playbook like you'd edit a source file. "
                "Get the current code with playbook_get_definition, then either "
                "pass the complete new source via code=, or make a targeted "
                "change via old=/new= (the old snippet must match exactly one "
                "place in the current code; include surrounding lines to make "
                "it unique). Snapshots a version, compiles, validates, then "
                "replaces the definition. Legacy: definition_yaml= replaces "
                "from full YAML. Pass exactly one mode."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Existing playbook name"},
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
                },
                "required": ["name"],
            },
        ),
        _playbook_edit,
    ))

    return tools
