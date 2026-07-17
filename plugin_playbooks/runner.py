"""PlaybookRunner — executes playbook runs step by step.

This is the core execution engine. Each step kind has its own handler.
DBOS integration is abstracted behind this module — no DBOS decorators
leak outside.

For v1, we use a simple async execution model with DB persistence at
each step boundary. DBOS integration can be added later for crash recovery.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from jinja2 import StrictUndefined, Undefined
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from luna_sdk import EventBus, PluginContext, ToolRegistry, message_source

from .definition import OnError, PlaybookDef, StepDef, StepKind
from .models import Playbook, PlaybookRun, PlaybookStepRun

log = logging.getLogger("luna.playbooks.runner")

_SANDBOX_ENV = SandboxedEnvironment(undefined=StrictUndefined)

# 008.006: how often a live run emits `activity.heartbeat`. Clients derive
# `running = (now - lastBeat) < TTL` with TTL ≈ 8s, so a missed completion
# (crash) self-clears. Referenced as a module global so tests can patch it.
HEARTBEAT_INTERVAL = 2.5

# Contextvar set while a playbook run is executing — plugin-internal (step
# scoping, run introspection). Cross-plugin origin tagging goes through the
# SDK `message_source` contextvar instead (E11, 009.001).
_active_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_active_run_id", default=None
)


def _playbook_origin_scope(playbook: Any):
    """Bind the billing origin for a playbook run and everything it derives
    (luna-service 048): root_action_type=playbook_run + a stable playbook id.
    A scheduler-initiated run already carries channel=scheduler + the trigger
    id on the outer scope; that job id wins (the trigger is the cost driver),
    while this run keeps a plain (non-scheduler) channel so user-initiated
    playbooks land in the Playbooks usage section. No-op on older luna core."""
    from contextlib import nullcontext
    try:
        from luna_sdk import billing_origin_scope
    except Exception:  # noqa: BLE001 — older core: degrade to no attribution
        return nullcontext()
    return billing_origin_scope(
        root_action_type="playbook_run",
        job_id=getattr(playbook, "name", None),
        prefer_outer_job_id=True,  # a scheduler trigger id (if any) wins
    )




class PlaybookRunner:
    """Executes playbook runs. Stateless — all state lives in the DB."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        tool_registry: ToolRegistry,
        events: EventBus,
        agent: Any = None,
        context: PluginContext | None = None,
    ) -> None:
        self._sf = session_factory
        self._tools = tool_registry
        self._events = events
        self._agent = agent
        # 009.001/phase03 (E6): conversation pinning reads/writes go through
        # the SDK context instead of luna.agent.runtime internals.
        self._ctx = context

    async def start_run(
        self,
        playbook: Playbook,
        inputs: dict[str, Any] | None = None,
        trigger: str | None = None,
        parent_run_id: Any = None,
    ) -> PlaybookRun:
        """Create a new run and begin executing it."""
        # 006.712: capture the originating conversation so agent_steps can
        # send_chat_message back to the right chat. playbook_run is called
        # inside a chat turn, where the contextvar is pinned; trigger/cron
        # runs have no origin and stay null.
        conversation_id = self._ctx.current_conversation_id if self._ctx else None

        async with self._sf() as session:
            run = PlaybookRun(
                playbook_id=playbook.id,
                playbook_version=playbook.version,
                trigger=trigger,
                inputs=inputs or {},
                status="running",
                parent_run_id=parent_run_id,
                conversation_id=conversation_id,
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)

        await self._events.emit("playbook.run.started", {
            "run_id": str(run.id),
            "playbook_name": playbook.name,
            "inputs": inputs,
            "trigger": trigger,
        })

        # 008.006: generic presence channel. The list/brain react to
        # `activity.*` (not `playbook.*`), so any long task can light the same
        # indicators. A heartbeat task beats while the run is alive; its
        # absence (crash/cancel) lets clients self-clear via TTL.
        activity_id = str(run.id)
        activity_label = playbook.display_name or playbook.name
        # 008.006: every activity.* event MUST carry the same identity. Clients
        # key presence on meta.playbook_name (the slug), so started/heartbeat/
        # completed have to agree — otherwise heartbeats refresh a different key
        # than `started` set, the slug entry goes stale after the TTL, and the
        # list badge blinks off mid-run (and `completed` can't clear it).
        activity_meta = {"playbook_name": playbook.name}
        await self._events.emit("activity.started", {
            "activity_id": activity_id,
            "kind": "playbook",
            "label": activity_label,
            "meta": activity_meta,
        })

        definition = PlaybookDef.model_validate(playbook.definition)
        context = _RunContext(
            run_id=run.id,
            inputs=inputs or {},
            step_outputs={},
            conversation_id=conversation_id,
        )

        token = _active_run_id.set(str(run.id))
        source_token = message_source.set("playbook")
        heartbeat_task = asyncio.create_task(
            self._activity_heartbeat(activity_id, activity_label, activity_meta)
        )
        try:
            if not definition.steps:
                raise ValueError(
                    f"Playbook '{playbook.name}' has no steps — nothing to execute. "
                    "Add steps before running."
                )
            with _playbook_origin_scope(playbook):
                await self._execute_steps(definition.steps, context)
            await self._complete_run(run.id, "done")
            run.status = "done"
        except _PlaybookHalt as h:
            # 007.009.01: an explicit `halt` step ended the run early — success.
            log.info("playbook.run.halted run_id=%s reason=%s", run.id, h.reason)
            await self._complete_run(run.id, "done")
            run.status = "done"
        except _PlaybookAbort as e:
            await self._complete_run(run.id, "failed", error=str(e))
            run.status = "failed"
        except _PlaybookCancel:
            await self._complete_run(run.id, "cancelled")
            run.status = "cancelled"
        except Exception as e:
            log.exception("playbook.run.error run_id=%s", run.id)
            await self._complete_run(run.id, "failed", error=str(e))
            run.status = "failed"
        finally:
            # 008.006: stop the heartbeat and announce completion. The cancel
            # MUST run on success AND error (no leaked task); the explicit
            # `activity.completed` clears the indicator instantly, while the
            # TTL is only the crash-safety net.
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass
            await self._events.emit("activity.completed", {
                "activity_id": activity_id,
                "kind": "playbook",
                "label": activity_label,
                "status": run.status,
                "meta": activity_meta,
            })
            message_source.reset(source_token)
            _active_run_id.reset(token)

        return run

    async def _activity_heartbeat(
        self, activity_id: str, label: str, meta: dict[str, Any]
    ) -> None:
        """008.006: emit a steady `activity.heartbeat` while a run is alive.

        Started in `start_run` and cancelled in its `finally`. Emit-then-sleep
        so the first beat fires immediately after `activity.started`. Clients
        treat `running = (now - lastBeat) < TTL`; if this loop stops without an
        `activity.completed` (crash), the indicator clears on its own. `meta`
        mirrors `activity.started` so every beat refreshes the same presence key.
        """
        while True:
            await self._events.emit("activity.heartbeat", {
                "activity_id": activity_id,
                "kind": "playbook",
                "label": label,
                "meta": meta,
            })
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def dry_run(
        self,
        playbook: Playbook,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Simulate a run WITHOUT side effects: real control flow (loops,
        conditions, parallel, subtask, templates, expressions) but every
        effectful leaf (tool_call / agent_step / llm_step / wait_*) is stubbed.
        Writes nothing to the DB. Returns a trace of resolved args / branches /
        iteration counts — the playbook "test run".
        """
        definition = PlaybookDef.model_validate(playbook.definition)
        ctx = _RunContext(
            run_id=uuid.uuid4(),
            inputs=inputs or {},
            step_outputs={},
        )
        ctx.dry = True
        status = "done"
        error: str | None = None
        try:
            if not definition.steps:
                raise ValueError(
                    f"Playbook '{playbook.name}' has no steps — nothing to simulate."
                )
            await self._execute_steps(definition.steps, ctx)
        except _PlaybookHalt:
            status = "done"  # early return is a success in dry-run too
        except _PlaybookAbort as e:
            status, error = "failed", str(e)
        except Exception as e:  # noqa: BLE001
            status, error = "failed", str(e)
        return {
            "dry_run": True,
            "banner": (
                "DRY RUN — tool/LLM outputs are SIMULATED. Do NOT report any "
                "value below as a real result."
            ),
            "status": status,
            "error": error,
            # `trace` is execution order (one entry per step, with `output`).
            # `references` is the template namespace: it shows EXACTLY what each
            # `steps.<id>.*` resolves to. Reference results in templates as
            # steps.<step_id>.<field> (e.g. steps.my_loop.collected) — NEVER add
            # `.output`; that key only exists in the trace, not in templates.
            "hint": (
                "In templates, reference a step's result as "
                "steps.<step_id>.<field> (see `references` for the exact shape). "
                "Do NOT write steps.<id>.output.<field> — `output` only appears "
                "in the `trace` list below, not in the template namespace."
            ),
            "references": _nested_view(ctx.step_outputs),
            "trace": ctx.trace,
        }

    async def cancel_run(self, run_id: Any) -> None:
        """Cancel a running playbook."""
        async with self._sf() as session:
            run = await session.get(PlaybookRun, run_id)
            if run and run.status == "running":
                run.status = "cancelled"
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()

    async def _execute_steps(
        self,
        steps: list[StepDef],
        ctx: _RunContext,
    ) -> None:
        for step in steps:
            await self._execute_step(step, ctx)

    async def _execute_step(self, step: StepDef, ctx: _RunContext) -> Any:
        """Execute a single step, handling retries and errors."""
        if ctx.dry:
            return await self._execute_step_dry(step, ctx)
        step_run = await self._create_step_run(ctx.run_id, step)
        ctx.current_step_run_id = step_run.id
        attempt = 0
        max_retries = step.retry.max if step.retry else 0

        while True:
            try:
                await self._update_step_status(step_run.id, "running")
                await self._events.emit("playbook.step.started", {
                    "run_id": str(ctx.run_id),
                    "step_id": step.id,
                    "step_kind": step.kind.value,
                })

                result = await self._dispatch_step(step, ctx)
                ctx.step_outputs[step.id] = result

                await self._complete_step(
                    step_run.id, "done", outputs=result,
                    inputs=ctx.step_inputs.get(step.id),
                )
                await self._events.emit("playbook.step.completed", {
                    "run_id": str(ctx.run_id),
                    "step_id": step.id,
                    "outputs": result,
                })
                return result

            except (_PlaybookHalt, _PlaybookCancel) as control:
                # 007.009.01: control-flow signals are NOT step failures — mark
                # the step done and let the signal propagate to start_run.
                await self._complete_step(
                    step_run.id, "done",
                    outputs=ctx.step_outputs.get(step.id),
                    inputs=ctx.step_inputs.get(step.id),
                )
                raise control
            except Exception as e:
                attempt += 1
                await self._update_step_retry(step_run.id, attempt)

                if attempt <= max_retries:
                    backoff = step.retry.backoff_seconds * (2 ** (attempt - 1))
                    log.info("playbook.step.retry step=%s attempt=%s backoff=%s", step.id, attempt, backoff)
                    await asyncio.sleep(min(backoff, 60))
                    continue

                await self._events.emit("playbook.step.failed", {
                    "run_id": str(ctx.run_id),
                    "step_id": step.id,
                    "error": str(e),
                    "retry_count": attempt,
                })

                if step.on_error == OnError.ABORT:
                    await self._complete_step(step_run.id, "failed", error=str(e))
                    raise _PlaybookAbort(f"Step '{step.id}' failed: {e}") from e
                elif step.on_error == OnError.CONTINUE:
                    await self._complete_step(step_run.id, "failed", error=str(e))
                    log.warning("playbook.step.continue_after_error step=%s error=%s", step.id, e)
                    return None
                elif step.on_error == OnError.ESCALATE:
                    await self._complete_step(step_run.id, "waiting", error=str(e))
                    await self._events.emit("playbook.step.waiting", {
                        "run_id": str(ctx.run_id),
                        "step_id": step.id,
                        "reason": "escalation",
                    })
                    raise _PlaybookAbort(f"Step '{step.id}' escalated: {e}") from e

    async def _execute_step_dry(self, step: StepDef, ctx: _RunContext) -> Any:
        """Dry version of _execute_step: dispatch (with stubbed leaves), record
        a trace entry, no DB writes, no retries."""
        entry: dict[str, Any] = {"step_id": step.id, "kind": step.kind.value, "dry": True}
        try:
            result = await self._dispatch_step(step, ctx)
            ctx.step_outputs[step.id] = result
            if step.id in ctx.step_inputs:
                entry["resolved_inputs"] = ctx.step_inputs[step.id]
            entry["output"] = result
            ctx.trace.append(entry)
            return result
        except (_PlaybookHalt, _PlaybookCancel):
            entry["output"] = ctx.step_outputs.get(step.id)
            ctx.trace.append(entry)
            raise
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
            ctx.trace.append(entry)
            if step.on_error == OnError.CONTINUE:
                return None
            raise _PlaybookAbort(f"Step '{step.id}' failed: {e}") from e

    async def _dispatch_step(self, step: StepDef, ctx: _RunContext) -> Any:
        """Route to the correct step handler based on kind."""
        handlers = {
            StepKind.TOOL_CALL: self._run_tool_call,
            StepKind.AGENT_STEP: self._run_agent_step,
            StepKind.LLM_STEP: self._run_llm_step,
            StepKind.CONDITION: self._run_condition,
            StepKind.PARALLEL: self._run_parallel,
            StepKind.WAIT_FOR_APPROVAL: self._run_wait_for_approval,
            StepKind.WAIT_FOR_EVENT: self._run_wait_for_event,
            StepKind.SUBTASK: self._run_subtask,
            StepKind.LOOP: self._run_loop,
            StepKind.STATE: self._run_state,
            StepKind.HALT: self._run_halt,
        }
        handler = handlers.get(step.kind)
        if not handler:
            raise ValueError(f"Unknown step kind: {step.kind}")
        return await handler(step, ctx)

    async def _run_tool_call(self, step: StepDef, ctx: _RunContext) -> Any:
        """Execute a tool_call step — calls a registered tool by name."""
        if not step.tool:
            raise ValueError(f"Step '{step.id}': tool_call requires 'tool' field")

        args = _render_template_dict(step.args or {}, ctx, step_id=step.id)
        ctx.step_inputs[step.id] = args
        if ctx.dry:
            # No execution. Stub the result from the tool's output hints if any,
            # but always surface the RESOLVED args (proves templates rendered).
            return {"tool": step.tool, "resolved_args": args, "result": {"_dry": True}, "_dry": True}
        try:
            rt = self._tools.get(step.tool)
        except KeyError:
            raise ValueError(
                f"Step '{step.id}': unknown tool '{step.tool}' — it is not in "
                "the tool registry. The playbook definition references a tool "
                "that does not exist."
            ) from None
        result = await rt.handler(**args)
        return {"tool": step.tool, "result": result}

    async def _run_agent_step(self, step: StepDef, ctx: _RunContext) -> Any:
        """Execute an agent_step — full LLM turn via run_turn()."""
        if not step.prompt:
            raise ValueError(f"Step '{step.id}': agent_step requires 'prompt' field")
        if ctx.dry:
            # Render the prompt (exercises templates) but never call the model.
            rendered = _render_template(step.prompt, ctx, step_id=step.id)
            ctx.step_inputs[step.id] = {"prompt": rendered[:2000]}
            return _stub_from_schema(step.output_schema)
        # 008.993 (E10): the agent is injected as ctx.agent (the sub-agent/turn
        # facade) at on_load — no more building one here from luna.agent.*.
        agent = self._agent
        if not agent:
            raise RuntimeError(
                "agent_step requires an injected agent (ctx.agent) but none was "
                "provided to the PlaybookRunner."
            )

        rendered_prompt = _render_template(step.prompt, ctx, step_id=step.id)
        ctx.step_inputs[step.id] = {"prompt": rendered_prompt[:2000]}
        # 006.712 → 009.001/phase03: the agent facade binds (and restores) the
        # originating conversation for the turn, so send_chat_message and
        # approvals resolve to the right chat — no runtime contextvar poking.
        result, usage = await agent.run_turn(
            rendered_prompt,
            output_schema=step.output_schema,
            tools=step.tools,
            memory_write=False,
            conversation_id=ctx.conversation_id,
        )

        # Write cost if available — target THIS step-run row by PK (a step in a
        # loop has many rows for the same step_id; a (run_id, step_id) query
        # would raise MultipleResultsFound on iteration 2+).
        await self._record_step_cost(ctx.current_step_run_id, usage)

        return result if isinstance(result, dict) else {"_raw": result}

    async def _run_llm_step(self, step: StepDef, ctx: _RunContext) -> Any:
        """Execute an llm_step — a RAW model call, no agent scaffolding.

        Delegates to agent.run_llm(): just the prompt to the model, no tools,
        memory, skills, or identity. Defaults to the summarization chain
        (Haiku) — cheap/fast for transforms (classify/extract/summarize/format).
        The LLM/router dependency lives in the agent layer; this runner only
        touches the agent seam it already uses for agent_step.
        """
        if not step.prompt:
            raise ValueError(f"Step '{step.id}': llm_step requires 'prompt' field")
        if ctx.dry:
            rendered = _render_template(step.prompt, ctx, step_id=step.id)
            if step.system:
                _render_template(step.system, ctx, step_id=step.id)
            ctx.step_inputs[step.id] = {"prompt": rendered[:2000]}
            return _stub_from_schema(step.output_schema)
        agent = self._agent
        if not agent:
            raise RuntimeError(
                "llm_step requires an injected agent (ctx.agent) but none was "
                "provided to the PlaybookRunner."
            )

        rendered_prompt = _render_template(step.prompt, ctx, step_id=step.id)
        ctx.step_inputs[step.id] = {"prompt": rendered_prompt[:2000]}
        rendered_system = (
            _render_template(step.system, ctx, step_id=step.id) if step.system else None
        )
        result, usage = await agent.run_llm(
            rendered_prompt,
            purpose=step.purpose or "summarization",
            model=step.model,
            system=rendered_system,
            output_schema=step.output_schema,
        )

        await self._record_step_cost(ctx.current_step_run_id, usage)

        return result if isinstance(result, dict) else {"_raw": result}

    async def _run_condition(self, step: StepDef, ctx: _RunContext) -> Any:
        """Evaluate a condition and branch."""
        if not step.when:
            raise ValueError(f"Step '{step.id}': condition requires 'when' field")

        result = _eval_expression(step.when, ctx)
        if result:
            if step.then:
                await self._execute_steps(step.then, ctx)
            return {"branch": "then", "condition": True}
        else:
            if step.else_:
                await self._execute_steps(step.else_, ctx)
            return {"branch": "else", "condition": False}

    async def _run_parallel(self, step: StepDef, ctx: _RunContext) -> Any:
        """Fan-out parallel branches, wait for all."""
        if not step.branches:
            return {"branches": []}

        async def _run_branch(branch: list[StepDef]) -> list[Any]:
            results = []
            for s in branch:
                r = await self._execute_step(s, ctx)
                results.append(r)
            return results

        tasks = [_run_branch(b) for b in step.branches]
        branch_results = await asyncio.gather(*tasks, return_exceptions=True)
        return {"branches": [
            r if not isinstance(r, Exception) else {"error": str(r)}
            for r in branch_results
        ]}

    async def _run_wait_for_approval(self, step: StepDef, ctx: _RunContext) -> Any:
        """Pause and wait for owner approval."""
        await self._events.emit("playbook.step.waiting", {
            "run_id": str(ctx.run_id),
            "step_id": step.id,
            "reason": "approval",
        })
        # For v1: auto-approve after emitting the event.
        # Full approval integration comes when plugin_approvals is wired.
        return {"approved": True, "auto": True}

    async def _run_wait_for_event(self, step: StepDef, ctx: _RunContext) -> Any:
        """Wait for a matching bus event."""
        if not step.event:
            raise ValueError(f"Step '{step.id}': wait_for_event requires 'event' field")

        await self._events.emit("playbook.step.waiting", {
            "run_id": str(ctx.run_id),
            "step_id": step.id,
            "reason": "event",
        })
        # For v1: return immediately with a stub.
        # Full event waiting needs a future/queue pattern on the bus.
        return {"event": step.event, "received": False, "stub": True}

    async def _run_subtask(self, step: StepDef, ctx: _RunContext) -> Any:
        """Invoke another playbook as a subtask."""
        if not step.playbook:
            raise ValueError(f"Step '{step.id}': subtask requires 'playbook' field")

        mapped_inputs = _render_template_dict(step.inputs_map or {}, ctx, step_id=step.id)
        ctx.step_inputs[step.id] = mapped_inputs

        async with self._sf() as session:
            stmt = select(Playbook).where(Playbook.name == step.playbook)
            target = (await session.execute(stmt)).scalar_one_or_none()

        if not target:
            raise ValueError(f"Subtask playbook '{step.playbook}' not found")

        if ctx.dry:
            sub_def = PlaybookDef.model_validate(target.definition)
            sub_ctx = _RunContext(run_id=uuid.uuid4(), inputs=mapped_inputs, step_outputs={})
            sub_ctx.dry = True
            try:
                await self._execute_steps(sub_def.steps, sub_ctx)
            except _PlaybookHalt:
                pass
            out: dict[str, Any] = {"subtask": step.playbook, "_dry": True, "steps": sub_ctx.trace}
            out.update(self._eval_returns(step, sub_ctx))
            return out

        sub_run = await self.start_run(
            target,
            inputs=mapped_inputs,
            trigger=f"subtask:{ctx.run_id}",
            parent_run_id=ctx.run_id,
        )
        out = {"subtask_run_id": str(sub_run.id), "status": sub_run.status}
        # 007.009.01: surface sub-workflow outputs to the parent so it can read
        # steps.<subtask_id>.<key>.
        if step.returns:
            sub_outputs = await self._load_run_step_outputs(sub_run.id)
            sub_ctx = _RunContext(
                run_id=sub_run.id, inputs=mapped_inputs, step_outputs=sub_outputs,
            )
            out.update(self._eval_returns(step, sub_ctx))
        return out

    def _eval_returns(self, step: StepDef, sub_ctx: _RunContext) -> dict[str, Any]:
        if not step.returns:
            return {}
        resolved: dict[str, Any] = {}
        for key, expr in step.returns.items():
            resolved[key] = _eval_expression(expr, sub_ctx, step_id=step.id)
        return resolved

    async def _load_run_step_outputs(self, run_id: Any) -> dict[str, Any]:
        """Build a {step_id: outputs} map from a completed run's step rows
        (last write wins, matching the in-memory step_outputs of a live run)."""
        async with self._sf() as session:
            rows = (await session.execute(
                select(PlaybookStepRun)
                .where(PlaybookStepRun.run_id == run_id)
                .order_by(PlaybookStepRun.started_at)
            )).scalars().all()
        out: dict[str, Any] = {}
        for r in rows:
            if r.outputs is not None:
                out[r.step_id] = r.outputs
        return out

    async def _run_loop(self, step: StepDef, ctx: _RunContext) -> Any:
        """Execute a loop: `over` a list (optionally with bounded concurrency),
        or `while`/`until` a condition. 007.009.01: `vars` persist across
        iterations (so a growing frontier works), `break_when` stops early, and
        the loop aggregates every state op into `state_timeline` for the viz.
        """
        if not step.body:
            raise ValueError(
                f"Step '{step.id}': loop has an empty body — nothing to "
                "iterate. Nest steps inside the loop's 'body'."
            )

        iterations = 0
        results: list[Any] = []
        # 007.005: per-iteration accumulator. `collect` is a Jinja expression
        # evaluated after each body pass (item vars still in scope); its native
        # result is appended here and exposed as steps.<loop_id>.collected.
        # 007.009.01: strict — an undefined ref (e.g. `.output` on a schemaless
        # llm_step) raises loudly instead of silently appending None.
        collected: list[Any] = []
        stopped: str | None = None
        state_log_start = len(ctx.state_log)

        def _collect_iteration(c: _RunContext = ctx) -> None:
            if step.collect:
                collected.append(
                    _eval_expression(step.collect, c, strict=True, step_id=step.id)
                )

        def _break_hit() -> bool:
            return bool(
                step.break_when
                and _eval_expression(step.break_when, ctx, strict=True, step_id=step.id)
            )

        if step.over is not None:
            # 006.712: `over` may be a literal list (used as-is) or a string
            # expression (evaluated). Agents kept writing [1,...,10] literals.
            if isinstance(step.over, (list, tuple)):
                items = list(step.over)
            else:
                items = _eval_expression(step.over, ctx, strict=True, step_id=step.id)
            if not isinstance(items, (list, tuple)):
                items = [items]
            items = list(items)
            capped = items[: step.max_iterations]
            if len(items) > step.max_iterations:
                stopped = "max_iterations"

            if (step.concurrency or 1) > 1:
                results, collected = await self._run_loop_concurrent(step, capped, ctx)
                iterations = len(capped)
            else:
                for item in capped:
                    ctx.step_outputs[f"{step.id}._item"] = item
                    ctx.step_outputs[f"{step.id}._index"] = iterations
                    if step.item_name:
                        ctx.extra_vars[step.item_name] = item
                        ctx.extra_vars[f"{step.item_name}_index"] = iterations
                    await self._execute_steps(step.body, ctx)
                    _collect_iteration()
                    iterations += 1
                    results.append(item)
                    if _break_hit():
                        stopped = "break"
                        break
                if step.item_name:
                    ctx.extra_vars.pop(step.item_name, None)
                    ctx.extra_vars.pop(f"{step.item_name}_index", None)
        elif step.while_ or step.until:
            # while: loop WHILE truthy. until: loop UNTIL truthy. Both share the
            # same body; state in `vars` carries across iterations.
            while True:
                if iterations >= step.max_iterations:
                    stopped = "max_iterations"
                    log.warning(
                        "playbook.loop.max_iterations step=%s max=%s",
                        step.id, step.max_iterations,
                    )
                    break
                ctx.step_outputs[f"{step.id}._index"] = iterations
                if step.while_ is not None:
                    if not _eval_expression(step.while_, ctx, strict=True, step_id=step.id):
                        break
                else:
                    if _eval_expression(step.until, ctx, strict=True, step_id=step.id):
                        break
                await self._execute_steps(step.body, ctx)
                _collect_iteration()
                iterations += 1
                if _break_hit():
                    stopped = "break"
                    break
        else:
            raise ValueError(
                f"Step '{step.id}': loop requires 'over', 'while', or 'until'"
            )

        result: dict[str, Any] = {
            "iterations": iterations,
            "results": results,
            "collected": collected,
            "stopped": stopped,
        }
        # 007.009.01: hand the viz the ordered state ops that happened in this
        # loop (per-iteration), so a stack/queue can be replayed from one node.
        timeline = ctx.state_log[state_log_start:]
        if timeline:
            result["state_timeline"] = timeline
        return result

    async def _run_loop_concurrent(
        self, step: StepDef, items: list[Any], ctx: _RunContext,
    ) -> tuple[list[Any], list[Any]]:
        """Bounded-concurrency map over `items`. Each item body runs in an
        ISOLATED child context (its own vars/step_outputs snapshot), so there is
        no cross-item race; only the ordered `collect` result merges back. The
        validator forbids `state` mutation inside a concurrent body."""
        sem = asyncio.Semaphore(max(1, step.concurrency))

        async def _run_one(index: int, item: Any) -> Any:
            async with sem:
                child = _RunContext(
                    run_id=ctx.run_id,
                    inputs=ctx.inputs,
                    step_outputs=dict(ctx.step_outputs),
                    conversation_id=ctx.conversation_id,
                )
                child.dry = ctx.dry
                child.vars = dict(ctx.vars)
                child.step_outputs[f"{step.id}._item"] = item
                child.step_outputs[f"{step.id}._index"] = index
                if step.item_name:
                    child.extra_vars[step.item_name] = item
                    child.extra_vars[f"{step.item_name}_index"] = index
                await self._execute_steps(step.body, child)
                if ctx.dry:
                    ctx.trace.extend(child.trace)
                if step.collect:
                    return _eval_expression(
                        step.collect, child, strict=True, step_id=step.id,
                    )
                return None

        gathered = await asyncio.gather(
            *[_run_one(i, it) for i, it in enumerate(items)]
        )
        collected = list(gathered) if step.collect else []
        return list(items), collected

    async def _run_state(self, step: StepDef, ctx: _RunContext) -> Any:
        """007.009.01: apply one or more state ops to run-scoped `vars`.
        Powers stack (push_back/pop_back), queue (push_back/pop_front), set
        (add_unique), counter (incr/decr), and accumulators (append/extend/
        merge). Records each op (with a post-op snapshot) for the visualization.
        """
        if not step.state:
            raise ValueError(
                f"Step '{step.id}': state step requires at least one op in 'state'"
            )
        frames: list[dict[str, Any]] = []
        resolved: list[dict[str, Any]] = []
        for op in step.state:
            val = (
                self._eval_state_value(op.value, ctx, step.id)
                if op.value is not None else None
            )
            frame = _apply_state_op(op, val, ctx)
            frame["step_id"] = step.id
            frames.append(frame)
            ctx.state_log.append(frame)
            resolved.append({"op": op.op, "var": op.var})
        ctx.step_inputs[step.id] = {"ops": resolved}
        return {"ops": frames}

    def _eval_state_value(self, raw: Any, ctx: _RunContext, step_id: str) -> Any:
        """A state op `value` is a Jinja expression when it's a string (so
        `"[]"`, `"{{ vars.url }}"`, `"inputs.seed"`, `"1"` all work); non-string
        values are literals."""
        if not isinstance(raw, str):
            return raw
        return _eval_expression(raw, ctx, strict=True, step_id=step_id)

    async def _run_halt(self, step: StepDef, ctx: _RunContext) -> Any:
        """007.009.01: end the run early (success). Optional `when` guard; if it
        is falsy the run continues. `value` (rendered) becomes the run result."""
        if step.when is not None:
            if not _eval_expression(step.when, ctx, strict=True, step_id=step.id):
                return {"halted": False}
        value: Any = None
        if step.value is not None:
            value = (
                self._eval_state_value(step.value, ctx, step.id)
                if isinstance(step.value, str) else step.value
            )
        ctx.step_outputs[step.id] = {"halted": True, "value": value}
        raise _PlaybookHalt(value=value, reason=f"halt at {step.id}")

    # --- DB helpers ---

    async def _create_step_run(self, run_id: Any, step: StepDef) -> PlaybookStepRun:
        async with self._sf() as session:
            sr = PlaybookStepRun(
                run_id=run_id,
                step_id=step.id,
                step_kind=step.kind.value,
                status="pending",
            )
            session.add(sr)
            await session.commit()
            await session.refresh(sr)
            return sr

    async def _update_step_status(self, step_run_id: Any, status: str) -> None:
        async with self._sf() as session:
            sr = await session.get(PlaybookStepRun, step_run_id)
            if sr:
                sr.status = status
                if status == "running":
                    sr.started_at = datetime.now(timezone.utc)
                await session.commit()

    async def _record_step_cost(self, step_run_id: Any, usage: Any) -> None:
        """Attribute model cost to a specific step-run row (by PK).

        Keyed on the step-run id, NOT (run_id, step_id): a step inside a loop
        creates one row per iteration, so a (run_id, step_id) query would match
        many rows and raise MultipleResultsFound. The id is the only safe key.
        """
        if not (usage and getattr(usage, "cost_cents", None)):
            return
        if step_run_id is None:
            return
        async with self._sf() as session:
            sr = await session.get(PlaybookStepRun, step_run_id)
            if sr:
                sr.cost_cents = usage.cost_cents
                await session.commit()

    async def _update_step_retry(self, step_run_id: Any, count: int) -> None:
        async with self._sf() as session:
            sr = await session.get(PlaybookStepRun, step_run_id)
            if sr:
                sr.retry_count = count
                await session.commit()

    async def _complete_step(
        self,
        step_run_id: Any,
        status: str,
        outputs: Any = None,
        error: str | None = None,
        inputs: Any = None,
    ) -> None:
        async with self._sf() as session:
            sr = await session.get(PlaybookStepRun, step_run_id)
            if sr:
                sr.status = status
                sr.outputs = outputs
                # 007.009: persist the RESOLVED inputs (rendered args / prompt)
                # so playbook_status reads like a stack trace (template → value).
                if inputs is not None:
                    sr.inputs = inputs
                sr.error = error
                sr.completed_at = datetime.now(timezone.utc)
                await session.commit()

    async def _complete_run(
        self, run_id: Any, status: str, error: str | None = None,
    ) -> None:
        async with self._sf() as session:
            run = await session.get(PlaybookRun, run_id)
            if run:
                run.status = status
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()

        started_at = None
        completed_at = datetime.now(timezone.utc)
        async with self._sf() as session:
            run = await session.get(PlaybookRun, run_id)
            if run:
                started_at = run.started_at

        if started_at is not None and started_at.tzinfo is None:
            # sqlite returns naive datetimes; stored values are UTC
            started_at = started_at.replace(tzinfo=timezone.utc)
        duration_ms = int((completed_at - started_at).total_seconds() * 1000) if started_at else 0
        await self._events.emit("playbook.run.completed", {
            "run_id": str(run_id),
            "status": status,
            "duration_ms": duration_ms,
            "error": error,
        })


class _RunContext:
    """In-memory context for a single playbook run execution."""

    def __init__(
        self,
        run_id: Any,
        inputs: dict[str, Any],
        step_outputs: dict[str, Any],
        conversation_id: Any = None,
    ) -> None:
        self.run_id = run_id
        self.inputs = inputs
        self.step_outputs = step_outputs
        self.conversation_id = conversation_id
        # 006.712: loop `item_name` exposes the current item as a top-level
        # template var ({{ number }}) — friendlier than steps.<id>._item.
        self.extra_vars: dict[str, Any] = {}
        # 007.009: resolved per-step inputs (rendered args / prompt) for traces.
        self.step_inputs: dict[str, Any] = {}
        # 007.009.01: PK of the step-run row currently executing. A step that
        # runs inside a loop produces MANY rows with the same (run_id, step_id),
        # so cost/usage writes must target THIS row's id, never a (run_id,
        # step_id) query (which raises MultipleResultsFound on the 2nd iteration).
        self.current_step_run_id: Any = None
        # 007.009: dry-run mode — stub effectful leaves, no DB writes, collect
        # an in-memory trace instead of step rows.
        self.dry: bool = False
        self.trace: list[dict[str, Any]] = []
        # 007.009.01: run-scoped mutable state — survives across loop
        # iterations. A `state` step reads/writes these; templates see `vars.*`.
        self.vars: dict[str, Any] = {}
        # 007.009.01: append-only log of every state op (for the live
        # stack/queue visualization). Each loop slices its own portion into the
        # loop result's `state_timeline`.
        self.state_log: list[dict[str, Any]] = []

    def template_vars(self) -> dict[str, Any]:
        return {
            **self.extra_vars,
            # 007.009.01: views so `inputs.items`/`vars.items` read the KEY
            # "items", not the dict's .items() method (an input or queue named
            # `items`/`keys`/`values` is the obvious footgun).
            "inputs": _VarsView(self.inputs, "inputs"),
            "vars": _VarsView(self.vars),
            "steps": _nested_view(self.step_outputs),
        }


def _nested_view(flat: dict[str, Any]) -> dict[str, Any]:
    """Turn flat step_outputs into a nested dict for Jinja.

    Keys like ``count_loop._item`` become ``{"count_loop": {"_item": ...}}``.
    Non-dotted keys stay at the top level.

    007.009: a loop stores BOTH its result (``scan`` -> {collected, results,
    iterations}) and per-iteration vars (``scan._item`` / ``scan._index``).
    These share the ``scan`` slot, so we MERGE the result dict's fields up
    alongside ``_item``/``_index`` instead of burying the result under
    ``_value``. Without this, the documented ``steps.<loop_id>.collected``
    path resolved to Undefined ("'dict object' has no attribute 'collected'").
    A non-dict value that collides with dotted children still falls back to
    ``_value`` (it has no fields to merge).
    """
    result: dict[str, Any] = {}
    for key, value in flat.items():
        parent, dot, child = key.partition(".")
        if dot:
            slot = result.get(parent)
            if not isinstance(slot, dict):
                # promote a pre-existing scalar/non-dict into a container
                slot = {} if parent not in result else {"_value": result[parent]}
                result[parent] = slot
            slot[child] = value
        else:
            existing = result.get(key)
            if isinstance(existing, dict):
                if isinstance(value, dict):
                    # merge result fields up next to dotted children; keep any
                    # dotted child on a name clash (it's the loop-scoped var)
                    for k, v in value.items():
                        existing.setdefault(k, v)
                else:
                    existing["_value"] = value
            else:
                result[key] = value
    return result


_STUB_BY_TYPE: dict[str, Any] = {
    "string": "", "str": "", "text": "",
    "number": 0, "float": 0.0,
    "integer": 0, "int": 0,
    "boolean": False, "bool": False,
    "object": {}, "dict": {},
    "array": [], "list": [],
}


def _stub_for_type(spec: Any) -> Any:
    """Map a JSON-schema property (or shorthand type) to a typed placeholder."""
    if isinstance(spec, dict):
        return _STUB_BY_TYPE.get(str(spec.get("type", "")).lower(), "")
    return _STUB_BY_TYPE.get(str(spec).lower(), "")


def _stub_from_schema(schema: Any) -> dict[str, Any]:
    """Build a type-correct placeholder dict from an output_schema so dry-run
    downstream `steps.x.field` references resolve. Handles both the JSON-schema
    form ({properties: {...}}) and the shorthand form ({field: type})."""
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            return {**{k: _stub_for_type(v) for k, v in props.items()}, "_dry": True}
        # shorthand: {is_subscription: bool, service: str, ...}
        shorthand = {
            k: _stub_for_type(v) for k, v in schema.items()
            if k not in ("type", "required", "properties")
        }
        if shorthand:
            return {**shorthand, "_dry": True}
    return {"_dry_text": "(simulated model output)", "_dry": True}


class _PlaybookAbort(Exception):
    pass


class _PlaybookCancel(Exception):
    pass


class _PlaybookHalt(Exception):
    """007.009.01: raised by a `halt` step to end the run early (success)."""

    def __init__(self, value: Any = None, reason: str = "halt") -> None:
        super().__init__(reason)
        self.value = value
        self.reason = reason


class _VarsView:
    """007.009.01: a read-only attribute/item view over a template namespace
    (`vars` or `inputs`). `x.items` / `x['items']` both read the KEY — they do
    NOT return the underlying dict's `.items()` method (the #1 Jinja footgun when
    a queue/input is, naturally, called `items`/`keys`/`values`). Missing keys
    raise so StrictUndefined kicks in (loud, not silent None)."""

    __slots__ = ("_d", "_label")

    def __init__(self, d: dict[str, Any], label: str = "vars") -> None:
        object.__setattr__(self, "_d", d)
        object.__setattr__(self, "_label", label)

    def __getattr__(self, key: str) -> Any:
        try:
            return self._d[key]
        except KeyError as e:
            raise AttributeError(key) from e

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def __contains__(self, key: str) -> bool:
        return key in self._d

    def __iter__(self):
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __repr__(self) -> str:
        return f"{self._label}({self._d!r})"


def _snapshot(value: Any, *, cap: int = 50) -> Any:
    """A render-friendly, size-capped copy of a state value for the trace /
    visualization. Long lists are truncated; scalars/dicts pass through."""
    if isinstance(value, list):
        if len(value) > cap:
            return value[:cap] + [f"… +{len(value) - cap} more"]
        return list(value)
    return value


def _as_list(cur: Any) -> list[Any]:
    if isinstance(cur, list):
        return list(cur)
    if cur is None:
        return []
    return [cur]


def _apply_state_op(op: Any, val: Any, ctx: _RunContext) -> dict[str, Any]:
    """Apply one StateOp to ctx.vars and return a viz frame describing it.
    The frame's `after` is a size-capped snapshot of the collection post-op."""
    v = ctx.vars
    name = op.var
    cur = v.get(name)
    frame: dict[str, Any] = {"op": op.op, "var": name}

    if op.op == "set":
        v[name] = val
        frame["item"] = _snapshot(val)
    elif op.op in ("append", "push_back"):
        lst = _as_list(cur); lst.append(val); v[name] = lst
        frame["item"] = _snapshot(val)
    elif op.op == "push_front":
        lst = _as_list(cur); lst.insert(0, val); v[name] = lst
        frame["item"] = _snapshot(val)
    elif op.op == "extend":
        add = val if isinstance(val, list) else [val]
        v[name] = _as_list(cur) + list(add)
        frame["added"] = _snapshot(list(add))
    elif op.op in ("pop_back", "pop_front"):
        if not isinstance(cur, list) or not cur:
            raise ValueError(
                f"state: cannot {op.op} from empty/missing list 'vars.{name}' "
                "— guard the loop with a `while`/`until` length check"
            )
        lst = list(cur)
        item = lst.pop() if op.op == "pop_back" else lst.pop(0)
        v[name] = lst
        frame["item"] = _snapshot(item)
        if op.into:
            v[op.into] = item
            frame["into"] = op.into
    elif op.op == "add_unique":
        lst = _as_list(cur)
        added = val not in lst
        if added:
            lst.append(val)
        v[name] = lst
        frame["item"] = _snapshot(val)
        frame["added"] = added
    elif op.op == "incr":
        v[name] = (cur or 0) + (val if val is not None else 1)
        frame["item"] = v[name]
    elif op.op == "decr":
        v[name] = (cur or 0) - (val if val is not None else 1)
        frame["item"] = v[name]
    elif op.op == "merge":
        d = dict(cur) if isinstance(cur, dict) else {}
        if isinstance(val, dict):
            d.update(val)
        v[name] = d
    elif op.op == "delete":
        v.pop(name, None)
    else:  # pragma: no cover - schema-validated
        raise ValueError(f"state: unknown op '{op.op}'")

    frame["after"] = _snapshot(v.get(name))
    return frame


def _available_vars(ctx: _RunContext) -> str:
    """Human-readable list of template vars for error messages."""
    names = (
        sorted(ctx.extra_vars)
        + ["inputs." + k for k in ctx.inputs]
        + ["vars." + k for k in sorted(ctx.vars)]
        + ["steps." + k for k in sorted({k.split(".")[0] for k in ctx.step_outputs})]
    )
    return ", ".join(names) or "inputs.*, vars.*, steps.<step_id>.*"


def _render_template(template: str, ctx: _RunContext, *, step_id: str = "") -> str:
    """Render a Jinja template string against the run context.

    006.712: strings that contain Jinja syntax ({{ }} / {% %}) fail LOUD on
    errors — previously every exception was swallowed and the raw template
    passed through, so the step-agent received the literal
    "Say: Count {{ loop.number }}" and parroted it. Plain strings without
    Jinja syntax keep the lenient pass-through.
    """
    has_jinja = "{{" in template or "{%" in template
    try:
        tpl = _SANDBOX_ENV.from_string(template)
        return tpl.render(**ctx.template_vars())
    except Exception as e:
        if not has_jinja:
            return template
        raise ValueError(
            f"Step '{step_id}': template '{template}' failed to render "
            f"({type(e).__name__}: {e}). Available variables: "
            f"{_available_vars(ctx)}. Inside a loop use the loop's "
            "item_name, or steps.<loop_id>._item / ._index."
        ) from e


def _render_template_dict(
    d: dict[str, Any], ctx: _RunContext, *, step_id: str = "",
) -> dict[str, Any]:
    """Render all string values in a dict as Jinja templates."""
    result = {}
    for k, v in d.items():
        if isinstance(v, str):
            result[k] = _render_template(v, ctx, step_id=step_id)
        elif isinstance(v, dict):
            result[k] = _render_template_dict(v, ctx, step_id=step_id)
        else:
            result[k] = v
    return result


def _eval_expression(
    expr: str,
    ctx: _RunContext,
    *,
    strict: bool = False,
    step_id: str = "",
) -> Any:
    """Evaluate a Jinja expression and return a native Python value.

    Uses compile_expression() so range(), list operations, and arithmetic
    produce real Python objects (lists, ints, bools) instead of strings.
    Falls back to string rendering + type coercion when compilation fails.

    006.707: with strict=True (loop over/until), an undefined variable or a
    failing expression raises instead of coercing — previously a typo'd
    variable made `until` truthy and the loop silently ran 0 iterations
    while the run reported "done".
    """
    # Agents often write "{{ expr }}" (template syntax) where a bare
    # expression is expected — strip the braces so both forms work.
    expr = expr.strip()
    while expr.startswith("{{") and expr.endswith("}}"):
        expr = expr[2:-2].strip()

    vars_ = ctx.template_vars()
    try:
        # 007.009.01: compile_expression defaults to undefined_to_none=True,
        # which SILENTLY turns an undefined ref into None (the review-digest
        # bug). In strict mode keep the StrictUndefined so we can raise on it.
        compiled = _SANDBOX_ENV.compile_expression(expr, undefined_to_none=not strict)
        result = compiled(**vars_)
        # 007.009.01: in strict mode an undefined reference is a LOUD failure,
        # not a silently-collected None. This is the review-digest bug: a
        # schemaless llm_step returns {_raw: ...}, so `steps.x.output` resolves
        # to Undefined — strict collect now raises here instead of appending
        # null for every iteration while the run reports "done".
        if strict and isinstance(result, Undefined):
            raise ValueError(
                f"reference resolved to undefined "
                f"(a schemaless llm_step/agent_step has only `_raw`; "
                f"declare an output_schema to expose typed fields)"
            )
        if hasattr(result, "__iter__") and not isinstance(result, (str, dict)):
            return list(result)
        return result
    except Exception as e:
        if strict:
            available = ", ".join(
                ["inputs." + k for k in ctx.inputs] +
                ["steps." + k for k in sorted({k.split(".")[0] for k in ctx.step_outputs})]
            ) or "inputs.*, steps.<step_id>.*"
            raise ValueError(
                f"Step '{step_id}': expression '{expr}' failed to evaluate "
                f"({type(e).__name__}: {e}). Available variables: {available}. "
                "Inside a loop use steps.<loop_id>._item and steps.<loop_id>._index."
            ) from e

    try:
        rendered = _render_template("{{ " + expr + " }}", ctx)
        if rendered.lower() in ("true", "1", "yes"):
            return True
        if rendered.lower() in ("false", "0", "no", "none", ""):
            return False
        try:
            return int(rendered)
        except ValueError:
            pass
        try:
            return float(rendered)
        except ValueError:
            pass
        return rendered
    except Exception:
        return False
