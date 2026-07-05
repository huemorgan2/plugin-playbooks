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
from .validation import validate_definition


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
        definition_yaml: str,
        agent_autonomy: str = "agent_must_confirm",
    ) -> str:
        try:
            pb_def = parse_yaml(definition_yaml)
        except Exception as e:
            return json.dumps({"error": f"Invalid YAML: {e}"})

        async with session_factory() as session:
            existing = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if existing:
                return json.dumps({"error": f"Playbook '{name}' already exists"})
            all_pb = await _load_all_playbook_steps(session, exclude=name)

            import yaml as _yaml
            issues = validate_definition(
                _yaml.safe_load(definition_yaml),
                tool_registry=getattr(runner, "_tools", None), all_playbooks=all_pb,
                check_unknown_keys=True,
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
                definition=pb_def.model_dump(mode="json", exclude_none=True, by_alias=True),
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
                "Create a new playbook from the FULL YAML definition (steps, "
                "triggers, inputs all at once). This is how you author playbooks — "
                "write the whole thing, do not build it step by step. Validate the "
                "YAML first with playbook_validate if unsure."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique kebab-case name"},
                    "display_name": {"type": "string", "description": "Human-friendly name"},
                    "description": {"type": "string"},
                    "when_to_use": {"type": "string"},
                    "definition_yaml": {"type": "string", "description": "Full YAML definition"},
                    "agent_autonomy": {
                        "type": "string",
                        "enum": ["agent_must_confirm", "agent_may_trigger"],
                        "default": "agent_must_confirm",
                    },
                },
                "required": ["name", "definition_yaml"],
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
    async def _run(*, name: str, inputs: str = "{}") -> str:
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

        run = await runner.start_run(playbook, inputs=input_data, trigger="agent")

        result: dict = {
            "run_id": str(run.id),
            "playbook": name,
            "status": run.status,
        }

        if run.status == "failed":
            async with session_factory() as session:
                run_obj = await session.get(PlaybookRun, run.id)
                if run_obj and run_obj.completed_at:
                    result["error"] = (
                        "Playbook execution FAILED. Do NOT fabricate results. "
                        "Check the error details with playbook_status."
                    )
        elif run.status == "done":
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
            description="Trigger a playbook run with the given inputs.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "inputs": {"type": "string", "description": "JSON string of inputs"},
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

            return json.dumps({
                "run_id": run_id,
                "status": run.status,
                "steps": [{
                    "step_id": s.step_id,
                    "kind": s.step_kind,
                    "status": s.status,
                    "inputs": s.inputs,
                    "outputs": s.outputs,
                    "error": s.error,
                } for s in steps],
            })

    tools.append((
        ToolDef(
            name="playbook_status",
            description="Get the full step trace of a playbook run.",
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
            author=author,
            message=message,
            promoted_from=promoted_from,
        )
        session.add(v)
        return v

    async def _playbook_get_definition(*, name: str) -> str:
        import yaml as _yaml

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})

            return _yaml.dump(playbook.definition, default_flow_style=False, sort_keys=False)

    tools.append((
        ToolDef(
            name="playbook_get_definition",
            description=(
                "Get the full YAML of a playbook so you can edit it. Returns the "
                "whole definition (steps, triggers, inputs) with step IDs. Edit the "
                "YAML you get back and pass it to playbook_edit."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                },
                "required": ["name"],
            },
        ),
        _playbook_get_definition,
    ))

    # --- playbook_validate (the compiler) ---
    async def _validate(*, name: str = "", definition_yaml: str = "") -> str:
        import yaml as _yaml

        check_keys = False
        if definition_yaml:
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
            return json.dumps({"error": "Provide 'name' or 'definition_yaml'."})

        async with session_factory() as session:
            all_pb = await _load_all_playbook_steps(session, exclude=name or None)
        issues = validate_definition(
            defn, tool_registry=getattr(runner, "_tools", None), all_playbooks=all_pb,
            check_unknown_keys=check_keys,
        )
        errors = [i.to_dict() for i in issues if i.severity == "error"]
        warnings = [i.to_dict() for i in issues if i.severity == "warning"]
        return json.dumps({"ok": not errors, "errors": errors, "warnings": warnings})

    tools.append((
        ToolDef(
            name="playbook_validate",
            description=(
                "Statically check a playbook WITHOUT running it (the compiler). "
                "Returns ALL issues at once: schema errors, unknown keys, undefined "
                "{{inputs}}/{{steps}} references, use-before-define, bad loops, unknown "
                "tools, subtask cycles, and context-economy warnings. Pass a saved "
                "playbook 'name' OR a 'definition_yaml'. Run this before saving or running."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Saved playbook name"},
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

    # --- playbook_edit (whole-YAML edit-in-place) ---
    async def _playbook_edit(*, name: str, definition_yaml: str) -> str:
        try:
            pb_def = parse_yaml(definition_yaml)
        except Exception as e:
            return json.dumps({"error": f"Invalid YAML: {e}"})

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({
                    "error": f"Playbook '{name}' not found. Use playbook_propose to create it.",
                })

            all_pb = await _load_all_playbook_steps(session, exclude=name)
            import yaml as _yaml
            issues = validate_definition(
                _yaml.safe_load(definition_yaml),
                tool_registry=getattr(runner, "_tools", None), all_playbooks=all_pb,
                check_unknown_keys=True,
            )
            errors = [i.to_dict() for i in issues if i.severity == "error"]
            if errors:
                return json.dumps({
                    "error": "Edit rejected — the new definition is invalid.",
                    "issues": errors,
                })

            await _snapshot_version(
                session, playbook, author="agent", message="before whole-YAML edit",
            )
            data = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
            data["name"] = name  # never rename via edit
            playbook.definition = data
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
            chat_only=True,
            description=(
                "Change an existing playbook by rewriting its FULL YAML (edit the "
                "whole 'file' at once). This is the ONLY way to edit a playbook — get "
                "the current YAML with playbook_get_definition, change it, and pass "
                "the complete new YAML here. Snapshots a version, validates, then "
                "replaces the definition. Rejects invalid YAML. Do not edit playbooks "
                "step by step."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Existing playbook name"},
                    "definition_yaml": {"type": "string", "description": "Full new YAML definition"},
                },
                "required": ["name", "definition_yaml"],
            },
        ),
        _playbook_edit,
    ))

    return tools
