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
from .delegation import writer_identity
from .models import (
    Playbook,
    PlaybookEditTicket,
    PlaybookRun,
    PlaybookStepRun,
    PlaybookVersion,
    PlaybookWatch,
)
from .pblang import PlaybookCompileError, compile_playbook, generate_code
from .probes import preflight_note, run_preflight
from .provenance import envelope as _envelope
from .provenance import overview_hint as _overview_hint
from .provenance import row_envelope as _row_envelope
from .provenance import with_envelope as _with_envelope
from .publish import (
    announce_publish,
    ops_conversation_id,
    test_run_gate,
)
from .reference import LANGUAGE_CHEATSHEET, LANGUAGE_MINIREF
from .runner import InputTypeError
from .runner import active_run_id as _active_playbook_run
from .v2.checker import check as v2_check
from .v2.checker import resolve_format, sniff_format
from .v2.migrate import compare_effects as _compare_effects
from .v2.migrate import require_green_live_run as _require_green_live_run
from .v2.migrate import v1_effects as _v1_effects
from .v2.migrate import v2_effects as _v2_effects
from .v2.migrate import v2_groups as _v2_groups
from .v2.skill import PUBLISH_RULE
from .validation import validate_definition
from .versioning import (
    author_label,
    candidate_conflict,
    conflict_message,
    ensure_live_row,
    live_version_of,
    mint_version,
)
from .versioning import get_version_row as _tolerant_get_version_row_fn

_log = logging.getLogger("luna.plugin.playbooks.agent_tools")


def _gate_owner_line(gate: dict[str, Any]) -> str:
    """021: one ✓/✗ bullet per gate on the approval card, in owner words
    (vocabulary rule — internal gate codes never reach the owner's eyes)."""
    g, ok, note = gate.get("gate"), gate.get("ok"), gate.get("note", "")
    if g == "static_validation":
        return "Structure check passed" if ok else "Structure check failed"
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
    """The playbook's source — stored, or (pblang only) derived via codegen.
    plans/032 phase 04: a python playbook's code IS its definition; it is
    never NULL and never derived."""
    if playbook.code:
        return playbook.code
    if getattr(playbook, "format", "pblang") == "python":
        return playbook.code or ""
    return generate_code(PlaybookDef.model_validate(playbook.definition))


# plans/032 phase 04 — the python authoring path (docs/v2.md §7-§9).
_PY_REFERENCE_LINE = "python playbook — the playbook-authoring skill is the reference"
_FORMAT_PARAM = {
    "type": "string",
    "enum": ["pblang", "python"],
    "description": (
        "Playbook language. python: one `async def run(ctx, inputs)`; "
        "pblang: the `playbook(...)` DSL. Omitted: sniffed from the code."
    ),
}
_INPUTS_SCHEMA_PARAM = {
    "type": "string",
    "description": (
        "python only: JSON-schema object for the run inputs, e.g. "
        '{"type": "object", "properties": {"url": {"type": "string"}}}. '
        "Values are coerced to the declared types at intake."
    ),
}
_TRIGGERS_PARAM = {
    "type": "string",
    "description": (
        "python only: JSON list of triggers, e.g. "
        '[{"event": "email.received", "map": {"url": "{{ event.payload.url }}"}}]. '
        "Triggers activate when the playbook is published."
    ),
}


def _registry_tool_names(registry: Any) -> set[str] | None:
    """Every tool name the registry knows, or None when it cannot be listed
    (then the checker skips its unknown-tool rule)."""
    if registry is None:
        return None
    try:
        names: set[str] = set()
        for rt in registry.all():
            name = getattr(getattr(rt, "definition", None), "name", None) or getattr(rt, "name", None)
            if name:
                names.add(str(name))
        return names
    except Exception:  # noqa: BLE001 — a stub registry without .all()
        return None


def _parse_json_param(raw: Any, *, label: str, kind: type) -> tuple[Any, str | None]:
    """A JSON object/list tool parameter → (value, error)."""
    if raw is None or raw == "":
        return None, None
    value = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"{label} is not valid JSON: {e.msg}"
    if not isinstance(value, kind):
        want = "object" if kind is dict else "list"
        return None, f"{label} must be a JSON {want}"
    return value, None


def _python_check(
    code: str, *, name: str, version: int, inputs_schema: dict | None, registry: Any,
) -> tuple[Any, list[dict], list[dict]]:
    """Run the v2 checker; (result, error dicts, warning dicts)."""
    result = v2_check(
        code, name=name, version=version, inputs_schema=inputs_schema,
        tool_names=_registry_tool_names(registry),
    )
    errors = [i.to_dict() for i in result.issues if i.severity == "error"]
    warnings = [i.to_dict() for i in result.issues if i.severity != "error"]
    return result, errors, warnings


def _python_definition(
    *, name: str, summary: dict, triggers: list | None, inputs_schema: dict | None,
) -> dict:
    """The stored `definition` of a python playbook — identity + triggers +
    inputs schema + the checker summary (never a PlaybookDef)."""
    defn = {
        "name": name,
        "format": "python",
        "triggers": list(triggers or []),
        "inputs": inputs_schema,
        **summary,
    }
    defn["name"] = name  # the summary never carries identity; pin it
    defn["format"] = "python"
    return defn


def _validate_triggers(triggers: list | None) -> str | None:
    """Every trigger must be a TriggerDef (definition.py)."""
    from .definition import TriggerDef

    for t in triggers or []:
        try:
            TriggerDef.model_validate(t)
        except Exception as e:  # noqa: BLE001
            return f"invalid trigger {t!r}: {e}"
    return None


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


async def _ticket_seconds_left(session: AsyncSession, ticket: str) -> int:
    """plans/032 phase 04: seconds until a (valid) ticket expires — the
    rejected-write payload tells the agent how long it may keep retrying
    with the same ticket."""
    from datetime import datetime, timedelta, timezone

    row = (await session.execute(
        select(PlaybookEditTicket).where(PlaybookEditTicket.id == uuid.UUID(ticket))
    )).scalar_one_or_none()
    if row is None:
        return 0
    expiry = _aware(row.created_at) + timedelta(seconds=_TICKET_TTL_SECONDS)
    left = (expiry - datetime.now(timezone.utc)).total_seconds()
    return max(1, min(_TICKET_TTL_SECONDS, int(left)))


_EDIT_RETRY_TEXT = "fix and call playbook_edit again with this ticket — do NOT re-read"


def _parked_what(parked_on: Any) -> str:
    """plans/032 phase 07: `approval #<id>` / `event '<name>'` from `parked_on`."""
    po = parked_on if isinstance(parked_on, dict) else {}
    if po.get("kind") == "approval":
        return f"approval #{po.get('approval_id')}"
    if po.get("kind") == "event":
        return f"event '{po.get('event_name')}'"
    return "an external signal"


def _parked_message(parked_on: Any, wake_promised: bool) -> str:
    """playbook_run / playbook_run_candidate: the run parked (docs/v2.md §11)."""
    tail = (
        "you will be WOKEN when it finishes. Do NOT poll playbook_status, do NOT "
        "re-run the playbook, and do NOT report results yet."
        if wake_promised else
        "it resumes by itself; check playbook_status(run_id) later. Do NOT re-run "
        "the playbook, and do NOT report results yet."
    )
    return f"Parked on {_parked_what(parked_on)} — nothing to poll; {tail}"


def _raise_stub(error_type: Any, message: Any) -> dict[str, Any]:
    return {"_raise": {"type": str(error_type or "EffectError"), "message": str(message or "")}}


def _flag(value: Any) -> bool:
    """A boolean tool parameter as the runtime may hand it over: a bool, or
    the strings "true"/"false"/"1"/"0" (case-insensitive)."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


async def _stubs_from_recorded_run(
    session: AsyncSession, runner: Any, playbook: Playbook, run_id: str,
    target_fmt: str,
) -> tuple[dict[str, Any], dict[str, Any]] | str:
    """plans/032 phase 08 `stubs_from_run`: the per-occurrence stubs a
    recorded run of `playbook` yields, or an error string.

    A python run replays its journal: `done` → the recorded result under
    `"<id>#<n>"`, `failed`/`failed_handled` → a `{"_raise": {type, message}}`
    stub the dry loop raises as the recorded error (approve → the decision,
    wait_event → the payload, now/random → the values, log skipped; rows
    with no recorded outcome are left unstubbed). A pblang run replays its
    step rows in `started_at` order under `"<step id>#<n>"` AND
    `"<tool>#<n>"` (a tool row's `outputs["result"]`, other rows' outputs);
    a pblang TARGET also gets the bare keys v1's stub lookup reads.

    Returns (stubs, source) where `source` is `{run_id, version, format,
    status}` — the caller adds `occurrences_used`/`occurrences_unmatched`
    once the trace is in."""
    try:
        rid = uuid.UUID(str(run_id))
    except ValueError:
        return f"run {run_id} not found"
    run = await session.get(PlaybookRun, rid)
    if run is None:
        return f"run {run_id} not found"
    if run.playbook_id != playbook.id:
        other = await session.get(Playbook, run.playbook_id)
        return f"run {run_id} belongs to playbook '{other.name if other else run.playbook_id}'"
    run_fmt = getattr(run, "format", None) or "pblang"
    stubs: dict[str, Any] = {}
    if run_fmt == "python":
        try:
            journal = await runner._v2.journal.read(str(run.id))
        except KeyError:
            journal = []
        for e in journal[1:]:
            if e.get("kind") == "log" or not e.get("id"):
                continue
            key = f"{e['id']}#{int(e.get('occurrence') or 1)}"
            status = e.get("status")
            if status == "done":
                stubs[key] = e.get("result")
            elif status in ("failed", "failed_handled"):
                err = e.get("error") or {}
                stubs[key] = _raise_stub(err.get("type"), err.get("message"))
    else:
        rows = (await session.execute(
            select(PlaybookStepRun)
            .where(PlaybookStepRun.run_id == run.id)
            .order_by(PlaybookStepRun.started_at, PlaybookStepRun.id)
        )).scalars().all()
        seen: dict[str, int] = {}
        for r in rows:
            if r.status not in ("done", "failed", "failed_handled"):
                continue
            out = r.outputs if isinstance(r.outputs, dict) else {}
            tool = out.get("tool") if isinstance(out.get("tool"), str) else None
            if r.status == "done":
                value = out.get("result") if tool else (r.outputs if r.outputs is not None else out)
            else:
                value = _raise_stub("ToolError" if tool else "EffectError", r.error)
            names = [r.step_id] + ([tool] if tool and tool != r.step_id else [])
            for nm in names:
                seen[nm] = seen.get(nm, 0) + 1
                stubs[f"{nm}#{seen[nm]}"] = value
                if target_fmt == "pblang":
                    stubs.setdefault(nm, value)
    source = {
        "run_id": str(run.id), "version": run.playbook_version,
        "format": run_fmt, "status": run.status,
    }
    return stubs, source


async def _handled_step_keys(runner: Any, run: Any) -> set[str]:
    """plans/032 phase 08: the `"<id>#<n>"` keys of a python run's journal
    rows the code caught and proceeded past (`failed_handled`). Empty for a
    v1 run or when the runner has no journal for it."""
    if (getattr(run, "format", None) or "pblang") != "python":
        return set()
    loop = getattr(runner, "_v2", None)
    journal = getattr(loop, "journal", None)
    if journal is None:
        return set()
    try:
        entries = await journal.read(str(run.id))
    except KeyError:
        return set()
    except Exception:  # noqa: BLE001 — a status read never fails on the journal
        _log.exception("playbooks: journal read failed for run %s", run.id)
        return set()
    return {
        f"{e.get('id')}#{e.get('occurrence')}"
        for e in entries[1:] if e.get("status") == "failed_handled" and e.get("id")
    }


def _parked_hint(parked_on: Any) -> str:
    """playbook_status hint for a `parked` run."""
    po = parked_on if isinstance(parked_on, dict) else {}
    due = po.get("due_at")
    if po.get("kind") == "approval":
        return (
            f"parked on approval #{po.get('approval_id')} — nothing to poll. The "
            f"owner has the card; the run resumes by itself when they decide "
            f"(due {due})."
        )
    if po.get("kind") == "event":
        return (
            f"parked on event '{po.get('event_name')}' — nothing to poll. The run "
            f"resumes by itself when the event fires, or fails with EventTimeout "
            f"at {due}."
        )
    return "parked — nothing to poll; the run resumes by itself."


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

    # plans/028: wake-on-completion needs core's send_muted_message; without
    # it (old core, bare test ctx) playbook_run keeps the poll contract.
    _wake_capable = getattr(ctx, "send_muted_message", None) is not None

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
        format: str | None = None,
        inputs_schema: str | dict | None = None,
        triggers: str | list | None = None,
    ) -> str:
        # 0.14.0 (plans/002 phase 7): code is the ONLY authoring format.
        # definition_yaml is still a declared-nowhere kwarg so stale callers
        # get a steering hint instead of a TypeError.
        if definition_yaml:
            # the steering hint comes first: a stale definition_yaml caller
            # gets it whether or not it also passed code.
            if code and resolve_format(format or None, code, default="python")[0] == "python":
                return json.dumps({
                    "error": "definition_yaml is pblang only — a python "
                             "playbook is its code; pass inputs_schema= and "
                             "triggers= instead.",
                    "format": "python",
                })
            return json.dumps({
                "error": "YAML authoring was removed — write the playbook as "
                         "`code` (see the playbook-authoring skill).",
                "format": "pblang",
            })
        if not code:
            return json.dumps({"error": "Provide 'code' — the full playbook source."})
        # plans/032 phase 04 (docs/v2.md §9): explicit > sniff > default python.
        fmt, fmt_issue = resolve_format(format or None, code, default="python")
        if fmt_issue is not None:
            return json.dumps({
                "error": "Playbook format could not be resolved.",
                "format": fmt,
                "errors": [fmt_issue.to_dict()],
                "warnings": [],
            })
        stored_code: str | None = code

        pb_def = None
        py_schema: dict | None = None
        py_triggers: list | None = None
        if fmt == "python":
            py_schema, perr = _parse_json_param(inputs_schema, label="inputs_schema", kind=dict)
            if perr:
                return json.dumps({"error": perr, "format": fmt})
            py_triggers, perr = _parse_json_param(triggers, label="triggers", kind=list)
            if perr:
                return json.dumps({"error": perr, "format": fmt})
            perr = _validate_triggers(py_triggers)
            if perr:
                return json.dumps({"error": perr, "format": fmt})
        else:
            pb_def, err = _compile_code(code, name=name)
            if err:
                payload = json.loads(err)
                payload["format"] = fmt
                return json.dumps(payload)
            defn = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
            defn["name"] = name

        async with session_factory() as session:
            existing = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if existing and existing.status != "archived":
                return json.dumps({"error": f"Playbook '{name}' already exists"})
            # plans/032 phase 11: the archived row may still carry another
            # author's unpublished candidate — the takeover must not replace
            # it silently (same refusal as the edit path, minus the ticket).
            author = writer_identity()
            if existing:
                conflict = await candidate_conflict(session, existing, author)
                if conflict is not None:
                    return json.dumps({
                        "stage": "write",
                        "saved": False,
                        "error": conflict_message(name, conflict),
                        "conflict": conflict,
                    })

            if fmt == "python":
                # the version the checker names is the one about to be minted
                next_version = (existing.version if existing else 0) + 1
                check_result, errors, warnings = _python_check(
                    code, name=name, version=next_version, inputs_schema=py_schema,
                    registry=getattr(runner, "_tools", None),
                )
                if errors:
                    return json.dumps({
                        "error": "Playbook is invalid — fix these before it can be created.",
                        "format": fmt,
                        "errors": errors,
                        "warnings": warnings,
                        "issues": errors,
                    })
                defn = _python_definition(
                    name=name, summary=check_result.summary,
                    triggers=py_triggers, inputs_schema=py_schema,
                )
                new_display = display_name or name
                new_description = description
                new_when = when_to_use
                new_inputs = py_schema
            else:
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
                        "format": fmt,
                        "issues": errors,
                        "errors": errors,
                        "warnings": [],
                    })
                warnings = [i.to_dict() for i in issues if i.severity == "warning"]
                new_display = display_name or pb_def.display_name or name
                new_description = description or pb_def.description
                new_when = when_to_use or pb_def.when_to_use
                new_inputs = pb_def.inputs

            # plans/032 phase 04: propose = candidate, always. The new content
            # is minted as a version row named by candidate_version; nothing
            # goes live until playbook_publish (gate + card).
            if existing:
                # plans/017: an archived playbook no longer squats its name —
                # the proposal takes over its row (id kept so run history
                # survives; mint_version climbs above every stored row so
                # old runs stay attributed to their versions).
                playbook = existing
                playbook.failures_acked_version = None
                playbook.display_name = new_display
                playbook.description = new_description
                playbook.when_to_use = new_when
                playbook.agent_autonomy = agent_autonomy
                playbook.created_by = "agent"
                playbook.status = "enabled"
                if _live_version_of(playbook) is None:
                    # never published: the row content is the candidate's
                    playbook.inputs_schema = new_inputs
                    playbook.definition = defn
                    playbook.code = stored_code
                    playbook.format = fmt
                    playbook.manifest = manifest
                # phase 08: a live version in the OTHER language is fine —
                # the live fields keep their format; the candidate row below
                # carries its own (`format=fmt`) and publish flips the live
                # format via _apply_version_to_live.
                await session.flush()
            else:
                playbook = Playbook(
                    name=name,
                    display_name=new_display,
                    description=new_description,
                    when_to_use=new_when,
                    inputs_schema=new_inputs,
                    definition=defn,
                    code=stored_code,
                    format=fmt,
                    manifest=manifest,
                    version=0,  # mint_version issues v1
                    agent_autonomy=agent_autonomy,
                    created_by="agent",
                    status="enabled",
                )
                session.add(playbook)
                await session.flush()
            await mint_version(
                session, playbook,
                definition=defn, code=stored_code,
                manifest=manifest or playbook.manifest or "",
                # phase 11: `agent`, or `delegation:<id>` inside a delegation
                author=author, message="candidate",
                format=fmt,  # phase 08: the candidate row's own language
            )
            playbook.candidate_version = playbook.version
            await session.commit()
            await session.refresh(playbook)
            candidate_version = playbook.version
            live_version = _live_version_of(playbook)

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
        # plans/032 phase 04: the master's propose contract — a candidate,
        # never a live version; publish activates triggers and playbook_run.
        return json.dumps({
            "playbook_id": str(playbook.id),
            "name": name,
            "format": fmt,
            "status": "candidate_saved",
            "live_version": live_version,
            "candidate_version": candidate_version,
            "runnable_via": "playbook_run_candidate",
            "triggers_active": False,
            "publish_required": True,
            "validated": True,
            "warnings": warnings,
            # plans/032 phase 09: the pointer — this write was validated,
            # so the next step is a test, never another check.
            "next": (
                f"Candidate v{candidate_version} saved and validated. Test it "
                "with playbook_run_candidate (playbook_dry_run simulates it "
                f"first), then playbook_publish(name='{name}') to make it "
                f"live. {_overview_hint(name)}"
            ),
        })

    tools.append((
        ToolDef(
            name="playbook_propose",
            artifact_ref="playbook:{name}",
            description=(
                "Create a new playbook from its FULL source, written all at "
                "once — saved as a CANDIDATE (validated, not live): test it "
                "with playbook_run_candidate, then playbook_publish makes it "
                "live and activates its triggers. Pass `code` in one of two "
                "languages (format=): python — one `async def run(ctx, "
                "inputs)` using ctx.tool/ctx.llm/ctx.approve (the default; "
                "see the playbook-authoring skill); pblang — the "
                "`playbook(...)` DSL (playbook(...) header, then "
                "x = tool(...)/llm(...)/loop(...)/if_(...) steps). The code "
                "is checked, never executed."
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
                    "format": _FORMAT_PARAM,
                    "inputs_schema": _INPUTS_SCHEMA_PARAM,
                    "triggers": _TRIGGERS_PARAM,
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

        # plans/032 phase 04: propose = candidate — nothing runs live until
        # playbook_publish. No run row is written for the refusal.
        if _live_version_of(playbook) is None:
            return json.dumps({
                "error": (
                    f"Playbook '{name}' has no live version — candidate "
                    f"v{playbook.candidate_version} is not published. Run it "
                    "with playbook_run_candidate or publish it."
                ),
                "candidate_version": playbook.candidate_version,
                "runnable_via": "playbook_run_candidate",
                "publish_required": True,
            })

        # plans/032 phase 08 (master §2 Lifecycle): `manual_only` is refused
        # outright — no card, and never a hint to change the autonomy;
        # `agent_must_confirm` raises a PER-RUN owner card from inside the
        # run (the run parks on it), instead of telling the agent to grant
        # itself permanent autonomy.
        if playbook.agent_autonomy == AgentAutonomy.MANUAL_ONLY.value:
            return json.dumps({
                "status": "refused",
                "playbook": name,
                "reason": (
                    "This playbook is manual_only — the owner runs it from the "
                    "playbook page. Do not change its autonomy on your own."
                ),
            })
        needs_card = playbook.agent_autonomy == AgentAutonomy.AGENT_MUST_CONFIRM.value

        try:
            run = await runner.start_run_background(
                playbook, inputs=input_data, trigger="agent",
                # the flag travels only when it is set (runner seams predating it)
                **({"needs_owner_card": True} if needs_card else {}),
            )
        except InputTypeError as e:
            # plans/032 phase 04: loud intake — the rejection names the
            # input and the declared type; no run row exists.
            return json.dumps({
                "status": "rejected", "error": str(e),
                "input": e.input, "expected": e.expected,
            })
        # plans/032 phase 09: the envelope — a real run of the LIVE content;
        # `version` is the row's stamp (runner: live_version or version).
        env = _envelope(
            "real_run", side_effects=True,
            version=getattr(run, "playbook_version", None),
            version_role="live", run_id=str(run.id),
        )
        if needs_card and run.status == "parked":
            # the run outlives this call by definition — the wake delivers
            # the outcome (master "nothing to poll"); stamped regardless of
            # the core's wake capability so the promise is on the row.
            approval_id = (run.parked_on or {}).get("approval_id")
            async with session_factory() as session:
                row = await session.get(PlaybookRun, run.id)
                if row is not None:
                    row.wake_on_complete = True
                    await session.commit()
            return json.dumps(_with_envelope(env, {
                "run_id": str(run.id),
                "playbook": name,
                "status": "parked",
                "approval_id": approval_id,
                "message": (
                    f"run {run.id} waiting on owner card #{approval_id} — "
                    "tell the user; nothing to poll"
                ),
                "next": _overview_hint(name),
            }))
        waited = await runner.wait_for_run(run.id, timeout=wait_seconds)
        status = waited.status if waited else run.status

        # plans/028: the run outlived the wait window (or fire-and-forget) —
        # promise a wake instead of demanding polls. Stamp is durable; the
        # RunCompletionWake service delivers on completion, and the orphan
        # sweep honors it across restarts. Old cores (no send_muted_message)
        # can't deliver a wake, so they keep the poll contract.
        wake_promised = False
        parked_on = getattr(waited, "parked_on", None) if waited else None
        # plans/032 phase 07: a `parked` run has no task but finishes later —
        # the wake stamp covers it exactly like a running one.
        if status in ("running", "parked") and _wake_capable:
            async with session_factory() as session:
                row = await session.get(PlaybookRun, run.id)
                if row is not None and row.status in ("running", "parked"):
                    row.wake_on_complete = True
                    await session.commit()
                    wake_promised = True
                    status = row.status
                    parked_on = row.parked_on
                elif row is not None:
                    status = row.status  # finished during the stamp window

        result: dict = {
            "run_id": str(run.id),
            "playbook": name,
            "status": status,
        }
        if status == "parked":
            result["parked_on"] = parked_on
            result["message"] = (
                f"{_parked_message(parked_on, wake_promised)} {_overview_hint(name)}"
            )
        if playbook.candidate_version:
            result["note"] = (
                "This ran the LIVE version "
                f"({playbook.live_version or playbook.version}) — an "
                f"un-promoted candidate (v{playbook.candidate_version}) "
                "exists. Use playbook_run_candidate to test it, "
                "playbook_publish to make it live."
            )

        if status == "running" and wake_promised:
            result["message"] = (
                "The playbook is still executing in the background (this is "
                f"normal for runs longer than {int(wait_seconds)}s). You "
                "will be WOKEN with the result when it finishes — do NOT "
                "poll playbook_status, do NOT re-run the playbook, and do "
                "NOT report results yet. Finish anything else and end your "
                f"turn; a follow-up turn delivers the outcome. {_overview_hint(name)}"
            )
        elif status == "running":
            result["message"] = (
                "The playbook is still executing in the background (this is "
                f"normal for runs longer than {int(wait_seconds)}s). Poll "
                "playbook_status(run_id) to see step-by-step progress and "
                "final outputs. Do NOT re-run the playbook, and do NOT report "
                f"results until playbook_status shows status 'done'. {_overview_hint(name)}"
            )
        elif status == "failed":
            # plans/032 phase 04 (docs/v2.md §7): the run's one-liner is
            # readable HERE — a python playbook's `error` leads with it; v1
            # keeps its sentence and gains `error_detail`.
            async with session_factory() as session:
                row = await session.get(PlaybookRun, run.id)
            run_error = getattr(row, "error", None) if row is not None else None
            fabricate = (
                "Playbook execution FAILED. Do NOT fabricate results. "
                "Check the error details with playbook_status."
            )
            if getattr(playbook, "format", "pblang") == "python" and run_error:
                result["error"] = f"{run_error} {fabricate}"
            else:
                result["error"] = fabricate
                if run_error:
                    result["error_detail"] = run_error
            result["error_type"] = getattr(row, "error_type", None) if row is not None else None
            failed_at = getattr(row, "failed_at", None) if row is not None else None
            result["failed_at"] = failed_at.isoformat() if failed_at else None
        elif status == "done":
            async with session_factory() as session:
                steps = (await session.execute(
                    select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
                )).scalars().all()
                row = await session.get(PlaybookRun, run.id)
                result["step_results"] = {
                    s.step_id: s.outputs for s in steps if s.outputs
                }
                # plans/032 phase 08: what `run()` returned (python runs;
                # null for a pblang run)
                result["result"] = getattr(row, "result", None) if row is not None else None
                if not result["step_results"] and result["result"] is None:
                    result["warning"] = (
                        "Playbook completed but produced no step outputs. "
                        "Verify the playbook has working steps before "
                        "reporting results to the user."
                    )

        result["next"] = _overview_hint(name)
        return json.dumps(_with_envelope(env, result))

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
                "return their results directly (status 'done' + step_results; "
                "a python playbook's `result` is what its run() returned). "
                "If the result says status 'running', the playbook is still "
                "going and you will be WOKEN with the result when it "
                "finishes — do not poll, never re-run it, and never invent "
                "results. An 'agent_must_confirm' playbook raises a per-run "
                "owner card: the result says status 'parked' with the card "
                "id — tell the user, nothing to poll, never re-run it. A "
                "'manual_only' playbook is refused. The result opens with "
                "kind / side_effects / version / version_role / run_id — "
                "quote kind and version when you report it."
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
                            "(0–90, default 55). Use 0 to fire-and-forget; "
                            "you are woken with the result either way."
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
            # plans/032 phase 08: a caught effect failure (journal status
            # `failed_handled`, docs/v2.md §2.6) is shown as such on its own
            # row — with its error — and is never hoisted: a done run with
            # handled failures carries no `error`. The step row itself stays
            # `failed` (phase 06/07 pin that); the journal is the truth.
            handled = await _handled_step_keys(runner, run)
            shown = [
                "failed_handled" if s.status == "failed" and s.step_id in handled else s.status
                for s in steps
            ]
            step_errors = [
                s.error for s, st in zip(steps, shown) if st == "failed" and s.error
            ]
            # plans/032 phase 09: one select, hoisted — the `playbook` key
            # and every `next` hint name the playbook; the envelope is
            # derived from the ROW (is_test / trigger), never from the
            # playbook's current pointers.
            playbook = await session.get(Playbook, run.playbook_id)
            pb_name = playbook.name if playbook is not None else None
            env = _row_envelope(run)
            payload: dict = {
                "run_id": run_id,
                "playbook": pb_name,
                "status": run.status,
                "trigger": run.trigger,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                # plans/032 phase 08: `run()`'s return value (null for v1 and
                # unfinished/failed runs)
                "result": getattr(run, "result", None),
                "steps": [{
                    "step_id": s.step_id,
                    "kind": s.step_kind,
                    "status": st,
                    "inputs": s.inputs,
                    "outputs": s.outputs,
                    "error": s.error,
                } for s, st in zip(steps, shown)],
            }
            # plans/032 phase 04 (docs/v2.md §7): the run row's own error
            # (a python one-liner, or v1's abort text) wins over the last
            # step error; the other columns ride along.
            run_error = getattr(run, "error", None)
            if run_error or step_errors:
                payload["error"] = run_error or step_errors[-1]
            if run.status == "failed" or run_error:
                payload["error_type"] = getattr(run, "error_type", None)
                failed_at = getattr(run, "failed_at", None)
                payload["failed_at"] = failed_at.isoformat() if failed_at else None
                tb = getattr(run, "traceback", None)
                payload["traceback"] = (
                    "\n".join(tb.splitlines()[-20:]) if tb else None
                )
            overview = _overview_hint(pb_name or "?")
            if run.status == "parked":
                # plans/032 phase 07 (docs/v2.md §11): no task, nothing to
                # poll — the service resumes it on the decision / event.
                payload["parked_on"] = getattr(run, "parked_on", None)
                payload["hint"] = f"{_parked_hint(payload['parked_on'])} {overview}"
            elif run.status == "running":
                payload["hint"] = (
                    "Still running — poll playbook_status again in a bit. "
                    f"Completed steps above already show their outputs. {overview}"
                )
            elif run.status == "failed":
                # 012 phase 4: a failed run still recorded the REAL outputs
                # of every step that ran — steer the agent to reuse them as
                # dry-run stubs before it starts fixing from memory.
                # plans/032 phase 08: `stubs_from_run` replays them directly.
                # plans/032 phase 09: the overview pointer comes AFTER it.
                payload["hint"] = (
                    "Failed — but every step that ran recorded its real "
                    "output above. Reuse those shapes as `stubs` in "
                    "playbook_dry_run (keyed by step id) to reproduce the "
                    "failure before fixing. After saving a fix, "
                    f"playbook_dry_run(name='{pb_name or '?'}', "
                    f"version='candidate', stubs_from_run='{run_id}') "
                    "replays this run's recorded effect results against the "
                    f"candidate, per occurrence. {overview}"
                )
            elif run.status == "timed_out_unknown":
                # plans/032 phase 06/08 (docs/v2.md §6): the outcome of an
                # effect is unknown — the run is neither green nor a clean
                # failure; nothing here proves the side effect did not happen.
                payload["hint"] = (
                    "Outcome unknown — an effect was in flight when the "
                    "process died and its result was never recorded. Do NOT "
                    "assume it did or did not happen; check the target "
                    f"system before re-running. {overview}"
                )
            if run.status not in ("running", "parked"):
                # terminal (done / failed / cancelled / timed_out_unknown)
                payload["next"] = overview
            return json.dumps(_with_envelope(env, payload))

    tools.append((
        ToolDef(
            name="playbook_status",
            modes=["planning", "building"],
            description=(
                "Get the live state of a playbook run: overall status "
                "(running/parked/done/failed/cancelled), timing, `result` "
                "(what a python playbook's run() returned), and the full "
                "step-by-step trace with each step's outputs and errors. "
                "Poll this after playbook_run returns status 'running'. A "
                "'parked' run (waiting on an owner approval or an event) has "
                "nothing to poll — it resumes by itself. The result opens "
                "with kind / side_effects / version / version_role / run_id "
                "— quote kind and version when you report it."
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
            description="Cancel a running or parked playbook run.",
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

    # --- playbook_watch / playbook_watch_cancel (0.46.0, plans/029) ---
    _WATCH_TTL_DAYS = 7

    async def _watch(*, name: str, note: str = "") -> str:
        if not _wake_capable:
            return json.dumps({"error": (
                "This Luna core cannot deliver watch wakes — poll "
                "playbook_status instead."
            )})
        conv = getattr(ctx, "current_conversation_id", None)
        if conv is None:
            return json.dumps({"error": (
                "playbook_watch only works from a conversation turn."
            )})
        from datetime import datetime, timedelta, timezone as _tz
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            expires = datetime.now(_tz.utc) + timedelta(days=_WATCH_TTL_DAYS)
            existing = (await session.execute(
                select(PlaybookWatch).where(
                    PlaybookWatch.playbook_id == playbook.id,
                    PlaybookWatch.conversation_id == uuid.UUID(str(conv)),
                    PlaybookWatch.consumed_at.is_(None),
                )
            )).scalar_one_or_none()
            if existing:
                existing.expires_at = expires
                existing.note = note or existing.note
            else:
                session.add(PlaybookWatch(
                    playbook_id=playbook.id,
                    conversation_id=uuid.UUID(str(conv)),
                    note=note or None,
                    expires_at=expires,
                ))
            await session.commit()
        return json.dumps({
            "watching": name,
            "message": (
                f"You will be WOKEN here when '{name}' next finishes a run — "
                "any trigger. Do NOT poll playbook_status while waiting; end "
                "your turn. One-shot: the watch is consumed by the first "
                f"finished run, and expires after {_WATCH_TTL_DAYS} days. "
                "Cancel with playbook_watch_cancel."
            ),
        })

    tools.append((
        ToolDef(
            name="playbook_watch",
            description=(
                "Wake me when an existing playbook's next run finishes. "
                "One-shot: the first completed run (any trigger — webhook, "
                "schedule, owner, another chat) delivers a follow-up turn "
                "here with the outcome, instead of you polling. Use only "
                "for playbooks that already exist and are run by something "
                "else."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "note": {
                        "type": "string",
                        "description": (
                            "Optional reminder-to-self, echoed back in the "
                            "wake (why you are waiting / what to do next)."
                        ),
                    },
                },
                "required": ["name"],
            },
        ),
        _watch,
    ))

    async def _watch_cancel(*, name: str) -> str:
        conv = getattr(ctx, "current_conversation_id", None)
        if conv is None:
            return json.dumps({"error": (
                "playbook_watch_cancel only works from a conversation turn."
            )})
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            existing = (await session.execute(
                select(PlaybookWatch).where(
                    PlaybookWatch.playbook_id == playbook.id,
                    PlaybookWatch.conversation_id == uuid.UUID(str(conv)),
                    PlaybookWatch.consumed_at.is_(None),
                )
            )).scalar_one_or_none()
            if not existing:
                return json.dumps({
                    "cancelled": False,
                    "message": f"No active watch on '{name}' from this chat.",
                })
            await session.delete(existing)
            await session.commit()
        return json.dumps({"cancelled": True, "playbook": name})

    tools.append((
        ToolDef(
            name="playbook_watch_cancel",
            description=(
                "Cancel this chat's active playbook_watch on a playbook."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                },
                "required": ["name"],
            },
        ),
        _watch_cancel,
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
        require_run: bool | None = None,
    ) -> str:
        if (not agent_autonomy and not publish_autonomy
                and require_run is None):
            return json.dumps({
                "error": "Nothing to change — pass agent_autonomy, "
                         "publish_autonomy and/or require_run.",
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
            # plans/016 phase 6: switchable publish gate (Settings → Publish)
            if require_run is not None:
                playbook.publish_require_run = require_run
            await session.commit()
            req_run = playbook.publish_require_run
        result: dict[str, Any] = {
            "playbook": name,
            "old_autonomy": old,
            "new_autonomy": agent_autonomy or old,
            "old_publish_autonomy": old_publish,
            "new_publish_autonomy": publish_autonomy or old_publish,
            "publish_require_run": req_run,
            "status": "updated",
        }
        if agent_autonomy:
            # plans/032 phase 08: the autonomy change is not a one-off grant
            result["note"] = (
                f"This change is PERMANENT for every future run of '{name}' "
                "— it is not a one-off approval. To run once with the "
                "owner's consent call playbook_run: it raises a per-run card."
            )
        if publish_autonomy == "auto":
            # luna 098 removed the ops modes that once honored 'auto'; the
            # publish gates + approval card decide, not this flag.
            publish_note = (
                "publish_autonomy no longer changes publishing: every "
                "agent publish runs the machine gates and raises the "
                "owner's approval card."
            )
            result["note"] = (
                f"{result['note']} {publish_note}" if result.get("note") else publish_note
            )
        result["next"] = _overview_hint(name)
        return json.dumps(result)

    tools.append((
        ToolDef(
            name="playbook_set_autonomy",
            description=(
                "Change per-playbook autonomy — PERMANENTLY, for every "
                "future run (never a one-off approval: to run once with the "
                "owner's consent call playbook_run). agent_autonomy = who "
                "may RUN it: 'agent_may_trigger' (agent runs freely), "
                "'agent_must_confirm' (each playbook_run raises a per-run "
                "owner card), 'manual_only' (agent cannot run it at all). "
                "publish_autonomy is legacy "
                "and no longer changes publishing — every agent publish "
                "runs the machine gates and raises the owner's approval "
                "card. require_run switches the test-run "
                "publish gate (Settings → Publish): off = the gate is still "
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
            live = _live_version_of(playbook)
            if live is None:
                return json.dumps({
                    "error": (
                        f"'{name}' has no live version — nothing is live, so "
                        "there is no failure digest to dismiss."
                    ),
                })
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

    def _live_version_of(playbook: Playbook) -> int | None:
        # plans/032 phase 04: one implementation (versioning.live_version_of);
        # None = no live version yet (candidate-only row).
        return live_version_of(playbook)

    async def _get_version_row(
        session: AsyncSession, playbook: Playbook, n: int,
    ) -> PlaybookVersion | None:
        return await _tolerant_get_version_row_fn(session, playbook, n)

    async def _ensure_live_row(
        session: AsyncSession, playbook: Playbook,
    ) -> PlaybookVersion | None:
        """Guarantee a version row exists for the current live content
        (None when nothing is live)."""
        return await ensure_live_row(session, playbook)

    def _row_format(row: PlaybookVersion) -> str:
        """phase 08: a version row's OWN language — the column, else the
        definition's marker, else pblang."""
        fmt = getattr(row, "format", None)
        if fmt in ("python", "pblang"):
            return fmt
        return "python" if (row.definition or {}).get("format") == "python" else "pblang"

    def _version_code(row: PlaybookVersion) -> str:
        """Source of a version row (stored, or — pblang — derived on read).
        phase 08: the row's own format decides, never the playbook's."""
        if row.code:
            return row.code
        if _row_format(row) == "python":
            return row.code or ""
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
        # phase 08: the promoted row's language becomes the live format
        if getattr(row, "format", None):
            playbook.format = row.format

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
            # plans/032 phase 04: the runner dispatches on it; phase 08: the
            # version row's OWN language (a candidate may differ from live)
            format=getattr(row, "format", None) or playbook.format,
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
    # files: every version's code, manifest, and runs, plus diffs.
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
                "code/manifest, run counts by "
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

    # --- playbook_overview (plans/032 phase 09: the truth surface) ---
    _OVERVIEW_CAP = 10

    def _req_field(req: Any, key: str, default: Any = None) -> Any:
        """Read a field off an ApprovalRequest (model or dict)."""
        if isinstance(req, dict):
            return req.get(key, default)
        return getattr(req, key, default)

    async def _overview(*, name: str) -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            live_n = _live_version_of(playbook)
            cand_n = playbook.candidate_version
            autonomy = playbook.agent_autonomy

            # what playbook_run would execute, and why / why not
            if live_n is None:
                executes = {
                    "version": None,
                    "reason": (
                        "no live version — candidate-only; playbook_run "
                        "refuses, use playbook_run_candidate"
                    ),
                }
            elif autonomy == AgentAutonomy.MANUAL_ONLY.value:
                executes = {
                    "version": live_n,
                    "reason": f"live version {live_n} — manual_only, playbook_run refuses",
                }
            elif autonomy == AgentAutonomy.AGENT_MUST_CONFIRM.value:
                executes = {
                    "version": live_n,
                    "reason": (
                        f"live version {live_n} — runs after the owner "
                        "approves the per-run card"
                    ),
                }
            else:
                executes = {"version": live_n, "reason": f"live version {live_n}"}

            # the candidate, with its newest test run
            candidate: dict[str, Any] | None = None
            if cand_n:
                row = await _get_version_row(session, playbook, cand_n)
                last_test = (await session.execute(
                    select(PlaybookRun)
                    .where(
                        PlaybookRun.playbook_id == playbook.id,
                        PlaybookRun.playbook_version == cand_n,
                        (PlaybookRun.is_test.is_(True))
                        | (PlaybookRun.trigger == "agent-candidate"),
                    )
                    .order_by(PlaybookRun.started_at.desc())
                    .limit(1)
                )).scalars().first()
                saved_at = None
                if row is not None:
                    ts = getattr(row, "last_edit_at", None) or row.created_at
                    saved_at = ts.isoformat() if ts else None
                at = None
                if last_test is not None:
                    ts = last_test.completed_at or last_test.started_at
                    at = ts.isoformat() if ts else None
                candidate = {
                    "version": cand_n,
                    "author": row.author if row is not None else None,
                    "saved_at": saved_at,
                    "last_test_run": (
                        {"run_id": str(last_test.id), "status": last_test.status, "at": at}
                        if last_test is not None else None
                    ),
                }

            # real runs of the live number (candidate test runs excluded)
            runs_of_live = 0
            if live_n is not None:
                rows = (await session.execute(
                    select(PlaybookRun.is_test, PlaybookRun.trigger).where(
                        PlaybookRun.playbook_id == playbook.id,
                        PlaybookRun.playbook_version == live_n,
                    )
                )).all()
                runs_of_live = sum(
                    1 for is_test, trig in rows
                    if not is_test and trig != "agent-candidate"
                )

            # parked runs (never finished / failed — docs/v2.md §11)
            parked_rows = (await session.execute(
                select(PlaybookRun)
                .where(
                    PlaybookRun.playbook_id == playbook.id,
                    PlaybookRun.status == "parked",
                )
                .order_by(PlaybookRun.started_at.desc())
            )).scalars().all()
            parked_runs = [
                {"run_id": str(r.id), "parked_on": getattr(r, "parked_on", None)}
                for r in parked_rows
            ]

            # pending approvals: (a) every parked-on-approval row, (b) the
            # approval system's pending list when the core exposes one
            pending: list[dict[str, Any]] = []
            by_id: dict[str, dict[str, Any]] = {}
            for r in parked_rows:
                po = getattr(r, "parked_on", None)
                if not isinstance(po, dict) or po.get("kind") != "approval":
                    continue
                aid = po.get("approval_id")
                if aid is None:
                    continue
                entry = {"approval_id": str(aid), "kind": "run", "run_id": str(r.id)}
                pending.append(entry)
                by_id[str(aid)] = entry
            list_pending = getattr(getattr(ctx, "approval", None), "list_pending", None)
            if list_pending is not None:
                try:
                    reqs = await list_pending()
                except Exception:  # noqa: BLE001 — a read never fails on the core
                    _log.exception("playbooks: approval.list_pending failed")
                    reqs = []
                for req in reqs or []:
                    if _req_field(req, "requested_by_plugin") != "plugin-playbooks":
                        continue
                    payload = _req_field(req, "payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    if name not in (payload.get("name"), payload.get("playbook")):
                        continue
                    rid = str(_req_field(req, "id"))
                    kind = _req_field(req, "kind") or "unknown"
                    if rid in by_id:
                        by_id[rid]["kind"] = kind
                        continue
                    run_ref = payload.get("run_id")
                    entry = {
                        "approval_id": rid, "kind": kind,
                        "run_id": str(run_ref) if run_ref else None,
                    }
                    pending.append(entry)
                    by_id[rid] = entry

            # versions, newest first — the playbook_versions entry shape
            # minus has_code / has_manifest / runs
            version_rows = (await session.execute(
                select(PlaybookVersion)
                .where(PlaybookVersion.playbook_id == playbook.id)
                .order_by(PlaybookVersion.version.desc())
            )).scalars().all()
            versions = [
                {
                    "version": r.version,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "author": r.author,
                    "message": r.message,
                    "promoted_from": r.promoted_from,
                    "live": r.version == live_n,
                    "candidate": r.version == cand_n,
                }
                for r in version_rows
            ]

        cap = _OVERVIEW_CAP
        if cand_n:
            next_text = (
                f"Candidate v{cand_n} is not live: test it with "
                f"playbook_run_candidate(name='{name}'), then "
                f"playbook_publish(name='{name}') to make it live."
            )
        elif parked_runs:
            next_text = (
                f"playbook_status(run_id='{parked_runs[0]['run_id']}') — a "
                "parked run has nothing to poll; it resumes by itself."
            )
        elif live_n is not None:
            next_text = f"playbook_run(name='{name}') executes live version {live_n}."
        else:
            next_text = "Nothing is live and no candidate exists — playbook_propose first."
        return json.dumps({
            "playbook": name,
            "format": getattr(playbook, "format", None) or "pblang",
            "playbook_run_executes": executes,
            "candidate": candidate,
            "runs_of_live_since_publish": runs_of_live,
            "parked_runs": parked_runs[:cap],
            "pending_approvals": pending[:cap],
            "autonomy": autonomy,
            "versions": versions[:cap],
            "more": {
                "parked_runs": max(0, len(parked_runs) - cap),
                "pending_approvals": max(0, len(pending) - cap),
                "versions": max(0, len(versions) - cap),
            },
            "next": next_text,
        })

    tools.append((
        ToolDef(
            name="playbook_overview",
            modes=["planning", "building"],
            description=(
                "The truth surface for ONE playbook — read it before "
                "describing a playbook's state. Derived, not raw rows: which "
                "version playbook_run executes and why (or why it refuses), "
                "the candidate (version, author, saved_at, its last test "
                "run), how many real runs the live version has had, parked "
                "runs, pending owner approvals, autonomy, the newest "
                "versions, and `next` — the one call that moves the "
                "playbook forward. Read-only; writes nothing."
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
        _overview,
    ))

    async def _version_read(
        *, name: str, version: int, include_runs: bool = True,
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
                "manifest, and its 10 most recent runs. Use "
                "playbook_runs for a run's full failure output."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "version": {"type": "integer", "description": "Version number to read"},
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
                    # plans/032 phase 08: the runtime and `run()`'s return
                    "format": getattr(r, "format", None) or "pblang",
                    "result": getattr(r, "result", None),
                }
                if r.status == "timed_out_unknown":
                    entry["error"] = getattr(r, "error", None)
                    entry["error_type"] = getattr(r, "error_type", None)
                    entry["hint"] = "outcome unknown — an effect's result was never recorded"
                if r.status == "failed":
                    # plans/032 phase 04 (docs/v2.md §7): the run-level
                    # error columns beside the per-step failures.
                    failed_at = getattr(r, "failed_at", None)
                    entry["error"] = getattr(r, "error", None)
                    entry["error_type"] = getattr(r, "error_type", None)
                    entry["failed_at"] = failed_at.isoformat() if failed_at else None
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
                # plans/032 phase 09: each entry is run-shaped → envelope
                # per row (the list itself is not a run and carries none)
                out_runs.append(_with_envelope(_row_envelope(r), entry))
            return json.dumps({
                "playbook": name,
                "count": len(out_runs),
                "filters": {"version": version, "status": status or None},
                "runs": out_runs,
                **({"note": "No runs match these filters."} if not out_runs else {}),
                "next": _overview_hint(name),
            })

    tools.append((
        ToolDef(
            name="playbook_runs",
            modes=["planning", "building"],
            description=(
                "List a playbook's runs, newest first — filter by version= "
                "and/or status= (running/parked/done/failed/cancelled). Each "
                "entry carries `format` and `result` (a python run's return "
                "value). Failed runs include every failed step's FULL error "
                "text and resolved inputs (read it like a CI log). Use "
                "playbook_status for one run's complete step-by-step trace. "
                "Each entry opens with kind / side_effects / version / "
                "version_role / run_id — quote kind and version when you "
                "report a run."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "version": {"type": "integer", "description": "Only runs of this version"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "running", "parked", "done", "failed", "cancelled",
                            "timed_out_unknown",
                        ],
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
    async def _validate(
        *, name: str = "", definition_yaml: str = "", code: str = "",
        format: str | None = None,
    ) -> str:
        if definition_yaml:
            # plans/023: YAML input removed — steering hint for stale callers.
            if (format or "") == "python" or (
                not format and code and sniff_format(code) == "python"
            ):
                return json.dumps({
                    "error": "definition_yaml is pblang only — a python "
                             "playbook is its code; pass code= instead.",
                    "format": "python",
                })
            return json.dumps({
                "error": "YAML validation was removed — pass code= (full "
                         "playbook source) or name= (a saved playbook) instead.",
                "format": "pblang",
            })
        if not code and not name:
            return json.dumps({"error": "Provide 'name' or 'code'."})
        # plans/032 phase 04: explicit > sniff > stored (name given) > python.
        pb: Playbook | None = None
        if name:
            async with session_factory() as session:
                pb = (await session.execute(
                    select(Playbook).where(Playbook.name == name)
                )).scalar_one_or_none()
            if not pb:
                return json.dumps({"error": f"Playbook '{name}' not found"})
        stored_fmt = getattr(pb, "format", None) if pb is not None else None
        if code or format:
            fmt, fmt_issue = resolve_format(
                format or None, code or None, stored=stored_fmt, default="python",
            )
        else:
            fmt, fmt_issue = (stored_fmt or "pblang"), None
        if fmt_issue is not None:
            return json.dumps({
                "ok": False, "format": fmt,
                "errors": [fmt_issue.to_dict()], "warnings": [],
                "saved": False,
                "note": "Format could not be resolved — nothing was checked further.",
            })
        saved_note = (
            "Validation only — NOTHING was saved. To persist a change, "
            "call playbook_edit (existing playbook) or playbook_propose "
            "(new playbook)."
        )
        if fmt == "python":
            src = code or (pb.code if pb is not None else "") or ""
            schema = pb.inputs_schema if (pb is not None and not code) else None
            _, errors, warnings = _python_check(
                src, name=name or "unnamed",
                version=(pb.version + 1) if pb is not None else 1,
                inputs_schema=schema, registry=getattr(runner, "_tools", None),
            )
            # no rider on python paths: the authoring skill is the reference
            return json.dumps({
                "ok": not errors, "format": fmt,
                "errors": errors, "warnings": warnings,
                "saved": False, "note": saved_note,
            })
        check_keys = False
        if code:
            pb_def, err = _compile_code(code, name=name or "unnamed")
            if err:
                payload = json.loads(err)
                return json.dumps({
                    "ok": False,
                    "format": fmt,
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
        else:
            defn = pb.definition

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
            "ok": not errors, "format": fmt, "errors": errors, "warnings": warnings,
            "saved": False,
            "note": saved_note,
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
                "Statically check a playbook WITHOUT running it. python "
                "(format='python', one `async def run(ctx, inputs)`): the "
                "checker — entry point, ctx usage, unknown tools, inputs "
                "not in the schema. pblang (the `playbook(...)` DSL): the "
                "compiler — compile errors, schema errors, unknown keys, "
                "undefined {{inputs}}/{{steps}} references, use-before-define, "
                "bad loops, unknown tools, subtask cycles, and context-economy "
                "warnings. Pass a saved playbook 'name' or playbook 'code' "
                "(preferred). Not needed after playbook_edit/playbook_propose "
                "— a green write is already validated."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Saved playbook name"},
                    "code": {"type": "string", "description": "Full playbook code to check"},
                    "format": _FORMAT_PARAM,
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

    # --- version target resolution (shared by dry_run and preflight) ---
    async def _resolve_target(
        session: AsyncSession, playbook: Playbook, version: str,
    ) -> tuple[Any, int] | str:
        """Resolve which content a tool acts on: 'auto' = candidate when
        one exists else live; or 'candidate' / 'live' / a version number.
        Returns (target, version_n) or an error string."""
        v = (version or "auto").strip().lower()
        if v == "auto":
            v = "candidate" if playbook.candidate_version else "live"
        if v == "live":
            live_n = _live_version_of(playbook)
            if live_n is None:
                # plans/032 phase 04: candidate-only row — nothing is live
                # yet, so the candidate is the only content to act on.
                v = "candidate"
            else:
                return playbook, live_n
        if v == "candidate":
            if not playbook.candidate_version:
                return (
                    f"'{playbook.name}' has no candidate — save an edit "
                    "first, or use version='live'."
                )
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

    # --- playbook_dry_run (the simulation harness) ---
    async def _dry_run(
        *, name: str, inputs: str = "{}", version: str = "auto",
        stubs: str | dict = "{}", stubs_from_run: str | None = None,
        compare: bool = False,
    ) -> str:
        try:
            input_data = json.loads(inputs) if isinstance(inputs, str) else inputs
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON inputs"})
        # plans/032 phase 12: `compare=true` needs the recorded run to
        # compare against — refused before anything is simulated.
        compare = _flag(compare)
        if compare and not stubs_from_run:
            return json.dumps({
                "error": "compare=true needs stubs_from_run=<run_id> — the "
                         "playbook's last green live run (playbook_runs(name, "
                         "version=<live>, status='done'), is_test false).",
                "dry_run": True,
            })
        try:
            stub_data = json.loads(stubs) if isinstance(stubs, str) else stubs
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON stubs"})
        if stub_data is None:
            stub_data = {}
        if not isinstance(stub_data, dict):
            return json.dumps({
                "error": "stubs must be a JSON object keyed by step id or "
                         "tool name.",
            })

        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})

            # 0.10.0: default to the candidate when one exists — dry-running
            # the thing you just edited is the point of the flow.
            resolved = await _resolve_target(session, playbook, version)
            if isinstance(resolved, str):
                return json.dumps({"error": resolved})
            target, tested = resolved
            fmt = "python" if getattr(target, "format", "pblang") == "python" else "pblang"

            # plans/032 phase 08: a recorded run's effect results as stubs,
            # per occurrence; explicit `stubs` win key by key.
            stubs_source: dict[str, Any] | None = None
            derived: dict[str, Any] = {}
            if stubs_from_run:
                got = await _stubs_from_recorded_run(
                    session, runner, playbook, str(stubs_from_run), fmt,
                )
                if isinstance(got, str):
                    return json.dumps({"error": got, "dry_run": True, "format": fmt})
                derived, stubs_source = got
                stub_data = {**derived, **stub_data}

            # plans/032 phase 12: the compared run's effect sequence, loaded
            # in the same session (its rows + the definition of the version
            # it ran); refused (error string, no traceback) unless it is the
            # last green live run and the target is python.
            compared: list[Any] | None = None
            compared_run: Any = None
            if compare:
                if fmt != "python":
                    return json.dumps({
                        "error": "compare=true needs a python target version "
                                 "(the migrated candidate) — the target is pblang.",
                        "dry_run": True, "format": fmt,
                    })
                compared_run = await session.get(PlaybookRun, uuid.UUID(str(stubs_from_run)))
                try:
                    _require_green_live_run(
                        compared_run.status,
                        bool(compared_run.is_test) or compared_run.trigger == "agent-candidate",
                        compared_run.playbook_version, _live_version_of(playbook),
                    )
                except ValueError as e:
                    return json.dumps({
                        "error": str(e), "dry_run": True, "format": fmt,
                        "compared_run_id": str(compared_run.id),
                        "compared_run_version": compared_run.playbook_version,
                    })
                if (getattr(compared_run, "format", None) or "pblang") == "python":
                    try:
                        recorded = await runner._v2.journal.read(str(compared_run.id))
                    except KeyError:
                        recorded = []
                    compared = _v2_effects(recorded)
                else:
                    rows = (await session.execute(
                        select(PlaybookStepRun)
                        .where(PlaybookStepRun.run_id == compared_run.id)
                        .order_by(PlaybookStepRun.started_at, PlaybookStepRun.id)
                    )).scalars().all()
                    vrow = await _get_version_row(session, playbook, compared_run.playbook_version)
                    definition = vrow.definition if vrow is not None else playbook.definition
                    compared = _v1_effects(definition, list(rows))

        is_candidate = bool(
            playbook.candidate_version and tested == playbook.candidate_version
        )
        # plans/032 phase 09: no row is written for a dry run — the envelope
        # names the resolved target; an explicit older number is `historical`.
        env = _envelope(
            "dry_run", side_effects=False, version=tested,
            version_role=(
                "candidate" if is_candidate
                else "live" if tested == _live_version_of(playbook)
                else "historical"
            ),
            run_id=None,
        )
        try:
            if fmt == "python":
                # plans/032 phase 05: the same segment loop in dry mode
                # (docs/v2.md §10) — stubs keyed `<call-site id>#<n>`.
                trace = await runner._v2.dry_run(
                    target, inputs=input_data, stubs=stub_data, version=tested,
                )
            else:
                trace = await runner.dry_run(target, inputs=input_data, stubs=stub_data)
        except InputTypeError as e:
            # plans/032 phase 04's loud intake, caught here for both formats:
            # a bad input fails before anything is simulated.
            return json.dumps({
                "status": "rejected", "error": str(e),
                "input": e.input, "expected": e.expected,
                "dry_run": True, "format": fmt, "tested_version": tested,
                "is_candidate": is_candidate,
            })
        if isinstance(trace, dict):
            trace["format"] = fmt
            trace["tested_version"] = tested
            trace["is_candidate"] = is_candidate
            if stubs_source is not None:
                if fmt == "python":
                    ran = set((trace.get("steps_ran") or {}).keys())
                    used = [k for k in derived if k in ran]
                else:
                    # v1 trace entries are keyed `step_id`; a stub reaches a
                    # step by its id or by the tool name the step calls
                    ids: set[str] = set()
                    for t in (trace.get("trace") or []):
                        if not isinstance(t, dict):
                            continue
                        ids.add(str(t.get("step_id") or t.get("id")))
                        out_t = t.get("output")
                        if isinstance(out_t, dict) and isinstance(out_t.get("tool"), str):
                            ids.add(out_t["tool"])
                    used = [k for k in derived if k.rpartition("#")[0] in ids or k in ids]
                stubs_source["occurrences_used"] = used
                stubs_source["occurrences_unmatched"] = [k for k in derived if k not in used]
                trace["stubs_source"] = stubs_source
            if compared is not None and compared_run is not None:
                # plans/032 phase 12: "reaches the same effects with the same
                # args" — the recorded run's effects against this dry run's
                # journal (gather members grouped from the target code).
                trace["comparison"] = _compare_effects(
                    compared,
                    _v2_effects(
                        trace.get("journal") or [],
                        _v2_groups(getattr(target, "code", None) or ""),
                    ),
                )
                trace["compared_run_id"] = str(compared_run.id)
                trace["compared_run_version"] = compared_run.playbook_version
            # plans/032 phase 09 (master §2 Dry run): a simulation is never
            # `done` at the tool boundary — the v1 runner keeps its own
            # status word (its direct callers pin it); v2 already says
            # `simulated` / `simulated_nothing_exercised`; `failed` stays.
            if trace.get("status") == "done":
                trace["status"] = "simulated"
            return json.dumps(_with_envelope(env, trace))
        return json.dumps(trace)

    tools.append((
        ToolDef(
            name="playbook_dry_run",
            timeout_seconds=60,
            description=(
                "Simulate a playbook run WITHOUT side effects. Real control "
                "flow runs; every effect is stubbed. The outputs are SIMULATED "
                "— never report them as real results. Python playbooks "
                "(`async def run(ctx, inputs)`): the same segment loop in dry "
                "mode — each `ctx.*` effect is answered from `stubs` keyed per "
                "occurrence `\"<call-site id>#<n>\"` (`\"fetch#1\"`; `\"fetch\"` "
                "for every occurrence; the id is the `_id=` you passed, else "
                "the assigned name) or by a placeholder that is truthy and "
                "iterates once; the result lists `steps_ran` per occurrence "
                "and `unreached_call_sites` (effects no branch reached), and a "
                "read the stubs do not cover is a `DryStubError` naming the "
                "stubs key to add. pblang playbooks: tool/LLM/wait steps are "
                "stubbed by step id or tool name and the result is a trace of "
                "resolved args, branches and loop iterations. Exercises the "
                "CANDIDATE version by default when one exists (version='live' "
                "or a number overrides). `stubs_from_run=<run_id>` replays a "
                "recorded run's real effect results as stubs, per occurrence; "
                "the result is SIMULATED and never counts as run evidence "
                "(`stubs_source` reports which occurrences were used). The "
                "result opens with kind / side_effects / version / "
                "version_role / run_id (kind 'dry_run', side_effects false, "
                "run_id null, status 'simulated') — quote kind and version "
                "when you report it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Playbook name"},
                    "inputs": {"type": "string", "description": "JSON string of inputs"},
                    "stubs": {
                        "type": "string",
                        "description": (
                            "JSON object of scripted results. Python: keyed "
                            "\"<call-site id>#<n>\" per occurrence, or "
                            "\"<call-site id>\" for every occurrence. pblang: "
                            "keyed by step id or tool name (step id wins). "
                            "Values are the raw result payload. Explicit "
                            "stubs override stubs_from_run key by key."
                        ),
                    },
                    "stubs_from_run": {
                        "type": "string",
                        "description": (
                            "A run_id of THIS playbook (playbook_status / "
                            "playbook_runs): its recorded effect results "
                            "become the stubs, per occurrence — a recorded "
                            "failure replays as the same error. Use it to "
                            "exercise a candidate fix against the exact run "
                            "that broke. Still a simulation."
                        ),
                    },
                    "compare": {
                        "type": "boolean",
                        "description": (
                            "Migration check (default false; needs "
                            "stubs_from_run = the playbook's last green live "
                            "run and a python target): the result gains "
                            "`comparison` {match, v1_count, v2_count, "
                            "mismatches[{class: order|missing|extra|kind|name|"
                            "args, position, v1, v2, paths}]}, "
                            "`compared_run_id`, `compared_run_version` — does "
                            "the candidate reach the same effects with the "
                            "same args as the recorded run. Refused for a "
                            "failed, test or non-live-version run."
                        ),
                    },
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
        format: str | None = None,
        inputs_schema: str | dict | None = None,
        triggers: str | list | None = None,
        replace_candidate: bool = False,
    ) -> str:
        snippet_mode = bool(old) or bool(new)
        modes = sum([bool(code), snippet_mode])
        # plans/032 phase 11: who is writing — `agent`, or `delegation:<id>`
        # inside a delegated turn; stamped on the row and used by the guard.
        author = writer_identity()

        # READ stage: no payload at all → manifest + code + fresh ticket.
        # (definition_yaml alone is a stale caller — handled below.)
        if modes == 0 and not definition_yaml:
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
                live_format = getattr(playbook, "format", "pblang") or "pblang"
                # phase 08: `format` is the language of what is being edited
                # (the candidate row's own), `live_format` the live one
                pb_format = _row_format(cand_row) if cand_row else live_format
                header = {
                    "stage": "read",
                    "editing": "candidate" if cand_row else "live",
                    "format": pb_format,
                    "live_format": live_format,
                    "version": playbook.version,
                    "live_version": _live_version_of(playbook),
                    "candidate_version": playbook.candidate_version,
                    # phase 11: who wrote the candidate being handed out
                    "candidate_author": cand_row.author if cand_row else None,
                    "ticket": str(t.id),
                    "expires_in_seconds": _TICKET_TTL_SECONDS,
                    "instructions": (
                        "Below: the manifest and current code as plain text. "
                        "The manifest is the bigger picture — read it before "
                        "changing things; it is not enforced, and if it is "
                        "outdated, update it (playbook_manifest_set — saves "
                        "onto the candidate; live only after publish). "
                        "Copy exact lines from the code block into old= for a "
                        "targeted edit. Then call playbook_edit again with "
                        "this ticket and exactly one of: code= (full source) "
                        "or old=/new= (targeted snippet). The ticket is "
                        "consumed by a successful write; a rejected write "
                        "keeps it valid (retry with the same ticket, do not "
                        "re-read). The ticket expires after 15 minutes. "
                        "Saving creates a CANDIDATE — "
                        "the live playbook keeps running unchanged until "
                        "playbook_publish."
                    ),
                }
                # phase 11: another author's unpublished candidate — warn
                # before the write, which would refuse anyway.
                conflict = await candidate_conflict(session, playbook, author)
                if conflict is not None:
                    header["conflict"] = conflict
                    header["instructions"] = (
                        "Another author's candidate exists — do not write; "
                        f"ask the owner. Candidate v{conflict['candidate_version']} "
                        f"was saved by {author_label(conflict['author'])} at "
                        f"{conflict['saved_at']} and is unpublished; a write "
                        "with this ticket is refused unless the owner says to "
                        "replace it (then pass replace_candidate=true). "
                        + header["instructions"]
                    )
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
                # plans/032 phase 04: pblang only — python's reference is
                # the authoring skill, so the frame carries one line.
                + "\n--- language reference ---\n"
                + (_PY_REFERENCE_LINE if header["format"] == "python" else LANGUAGE_MINIREF)
                + "\n--- end ---"
            )

        # 0.14.0 (plans/002 phase 7): YAML input removed — steering hint for
        # stale callers instead of a TypeError.
        if definition_yaml:
            return json.dumps({
                "error": "YAML editing was removed — pass code= (full source) "
                         "or old=/new= (targeted snippet) instead. "
                         "(definition_yaml is pblang only; a python playbook "
                         "is its code.)",
            })
        if modes != 1:
            return json.dumps({
                "error": "Provide exactly one of: 'code' or 'old'+'new'.",
            })
        if snippet_mode and not (old and new is not None):
            return json.dumps({"error": "Snippet edits need both 'old' and 'new'."})

        def _rejected(
            fmt: str, errors: list[dict], warnings: list[dict], seconds_left: int,
            *, extra: dict | None = None,
        ) -> str:
            # plans/032 phase 04 (docs/v2.md §9): a rejected write keeps
            # the ticket — the agent fixes and retries without re-reading.
            payload: dict[str, Any] = {
                "stage": "write",
                "saved": False,
                "format": fmt,
                "errors": errors,
                "warnings": warnings,
                "ticket": ticket,
                "ticket_still_valid": True,
                "expires_in_seconds": seconds_left,
                "retry": _EDIT_RETRY_TEXT,
            }
            if extra:
                payload.update(extra)
            if fmt != "python":
                payload["language_reference"] = LANGUAGE_CHEATSHEET
            return json.dumps(payload)

        # WRITE stage, part 1: ticket check + compile/check + validate.
        pb_def = None
        py_defn: dict | None = None
        py_schema: dict | None = None
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
            seconds_left = await _ticket_seconds_left(session, ticket)
            live_fmt = getattr(playbook, "format", "pblang") or "pblang"
            base_version = playbook.version
            # Edits build on the candidate when one exists (that's what the
            # read stage handed out), else on live.
            cand_row = None
            if playbook.candidate_version:
                cand_row = await _get_version_row(
                    session, playbook, playbook.candidate_version,
                )
            # phase 08: the stored format is the EDITED row's own language
            stored_fmt = _row_format(cand_row) if cand_row else live_fmt
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

            # plans/032 phase 04 (docs/v2.md §9): explicit > sniff > stored.
            # phase 08: a format change is ALLOWED — the candidate row carries
            # its own format; the live row keeps its language until publish.
            fmt, fmt_issue = resolve_format(
                format or None, code, stored=stored_fmt, default=stored_fmt,
            )
            if fmt_issue is not None:
                return _rejected(fmt or stored_fmt, [fmt_issue.to_dict()], [], seconds_left)
            stored_code = code
            if fmt == "python":
                py_schema, perr = _parse_json_param(
                    inputs_schema, label="inputs_schema", kind=dict,
                )
                if perr:
                    return json.dumps({"error": perr, "format": fmt})
                py_triggers, perr = _parse_json_param(triggers, label="triggers", kind=list)
                if perr:
                    return json.dumps({"error": perr, "format": fmt})
                perr = _validate_triggers(py_triggers)
                if perr:
                    return json.dumps({"error": perr, "format": fmt})
                base_defn = dict((cand_row.definition if cand_row else playbook.definition) or {})
                if py_schema is None:
                    py_schema = base_defn.get("inputs")
                if py_triggers is None:
                    py_triggers = list(base_defn.get("triggers") or [])
                check_result, errors, warnings = _python_check(
                    code, name=name, version=playbook.version + 1,
                    inputs_schema=py_schema, registry=getattr(runner, "_tools", None),
                )
                if errors:
                    return _rejected(fmt, errors, warnings, seconds_left)
                py_defn = _python_definition(
                    name=name, summary=check_result.summary,
                    triggers=py_triggers, inputs_schema=py_schema,
                )
                issues = []
            else:
                if inputs_schema not in (None, "") or triggers not in (None, ""):
                    return json.dumps({
                        "error": "inputs_schema= and triggers= are python only "
                                 "— a pblang playbook declares them in its "
                                 "playbook(...) header.",
                        "format": fmt,
                    })
                pb_def, err = _compile_code(code, name=name)
                if err:
                    compile_payload = json.loads(err)
                    # `error` keeps the pre-032 "does not compile" sentence
                    # that existing callers/tests key on; the ticket fields
                    # are the phase 04 additions.
                    return _rejected(
                        fmt, compile_payload["issues"], [], seconds_left,
                        extra={"error": compile_payload["error"]},
                    )
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
                warnings = [i.to_dict() for i in issues if i.severity == "warning"]
                if errors:
                    return _rejected(
                        fmt, errors, warnings, seconds_left,
                        extra={"error": "Edit rejected — the new definition is invalid."},
                    )

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
            # plans/032 phase 11: the candidate-conflict guard — BEFORE the
            # ticket is consumed, so a refused write keeps it valid. A
            # foreign candidate is only replaced on the owner's explicit
            # instruction (replace_candidate=true), and the row says so.
            conflict = await candidate_conflict(session, playbook, author)
            if conflict is not None and not replace_candidate:
                return json.dumps({
                    "stage": "write",
                    "saved": False,
                    "error": conflict_message(name, conflict),
                    "conflict": conflict,
                    "ticket": ticket,
                    "ticket_still_valid": True,
                })
            message = "candidate"
            if conflict is not None:
                message = (
                    f"candidate (replaced {conflict['author']} "
                    f"v{conflict['candidate_version']} on owner instruction)"
                )
            refusal = await _check_ticket(session, playbook, ticket, consume=True)
            if refusal:
                return json.dumps({"error": refusal})

            # 0.10.0: a save creates a CANDIDATE version row — live content
            # on the playbook row is not touched. One candidate max: the
            # pointer moves, the previous candidate row stays in history.
            # plans/032 phase 04: `_ensure_live_row` returns None on a
            # candidate-only row (nothing live to record).
            had_live = await _ensure_live_row(session, playbook) is not None
            if py_defn is not None:
                data = py_defn
            else:
                data = pb_def.model_dump(mode="json", exclude_none=True, by_alias=True)
                data["name"] = name  # never rename via edit
            await mint_version(
                session, playbook,
                definition=data, code=stored_code, manifest=playbook.manifest,
                author=author,  # phase 11: `agent` or `delegation:<id>`
                message=message,
                format=fmt,  # phase 08: the candidate row's own language
            )
            playbook.candidate_version = playbook.version
            if not had_live:
                # never published: the row content mirrors the candidate so
                # reads (_derive_code, GET /playbooks/{name}) show it.
                playbook.definition = data
                playbook.code = stored_code
                playbook.inputs_schema = data.get("inputs")
                playbook.format = fmt
            await session.commit()
            new_version = playbook.version
            live_version = _live_version_of(playbook)
            live_format = getattr(playbook, "format", "pblang") or "pblang"

        await events.emit("playbook.candidate.saved", {
            "name": name, "candidate_version": new_version,
        })
        if py_defn is None:
            warnings = [i.to_dict() for i in issues if i.severity == "warning"]
        if live_version is None:
            next_text = (
                "Do not call playbook_validate — this write was validated. "
                "No live version yet — triggers and playbook_run stay off "
                "until playbook_publish(name). Test the candidate with "
                "playbook_run_candidate, then publish to make it live."
            )
        else:
            next_text = (
                "Do not call playbook_validate — this write was validated. "
                "The LIVE playbook is unchanged — triggers and playbook_run "
                f"still execute version {live_version}. Test the candidate "
                + ("with playbook_run_candidate" if fmt == "python" else
                   "with playbook_dry_run (it targets the candidate by default)")
                + ", then call playbook_publish(name) to make it live. "
                "playbook_rollback restores the previous live version after "
                "a publish."
            )
            if fmt != live_format:
                # phase 08: the format changed — say what runs where
                next_text += (
                    f" Note: candidate v{new_version} is {fmt}; live "
                    f"v{live_version} stays {live_format} until publish."
                )
        result: dict[str, Any] = {
            "playbook": name,
            "format": fmt,
            "live_format": live_format,
            "status": "candidate_saved",
            "candidate_version": new_version,
            "live_version": live_version,
            "validated": True,
            "warnings": warnings,
            "next": f"{next_text} {_overview_hint(name)}",
        }
        return json.dumps(result)

    async def _playbook_edit(
        *,
        name: str,
        ticket: str = "",
        code: str = "",
        old: str = "",
        new: str = "",
        definition_yaml: str = "",
        format: str | None = None,
        inputs_schema: str | dict | None = None,
        triggers: str | list | None = None,
        replace_candidate: bool = False,
    ) -> str:
        return await _edit_impl(
            name=name, ticket=ticket, code=code, old=old, new=new,
            definition_yaml=definition_yaml, format=format,
            inputs_schema=inputs_schema, triggers=triggers,
            replace_candidate=bool(replace_candidate),
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
        "format": _FORMAT_PARAM,
        "inputs_schema": _INPUTS_SCHEMA_PARAM,
        "triggers": _TRIGGERS_PARAM,
        # plans/032 phase 11: a write over ANOTHER author's unpublished
        # candidate is refused; this is the explicit, owner-authorised way
        # through — the minted row's message names who was replaced.
        "replace_candidate": {
            "type": "boolean",
            "description": (
                "OWNER-authorised only — pass true only after the owner "
                "said to replace another author's unpublished candidate "
                "(the write refusal / read header name that author and "
                "version). Default false."
            ),
        },
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
                "Change an existing playbook (python or pblang — the stored "
                "format; format= must match it) — a two-step flow. STEP 1 "
                "(read): call with ONLY the name; you get the playbook's "
                "manifest (the bigger picture — read it before changing "
                "things; update it via playbook_manifest_set when it's "
                "outdated), the current code as plain readable text "
                "(copy old= snippets from it verbatim), and an edit ticket. "
                "STEP 2 (write): call again with that ticket plus "
                "exactly one of code= (full new source) or old=/new= (targeted "
                "snippet; 'old' must match exactly one place). "
                "The write checks the code (python: the checker; pblang: "
                "compile + validate), snapshots a version, then saves a "
                "CANDIDATE — live keeps running until playbook_publish. "
                "The ticket is consumed by a successful write; a rejected "
                "write keeps it valid — fix and retry with the same ticket. "
                "python only: inputs_schema= / triggers= replace the stored ones."
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
        # plans/033 (luna-fixer 2026-09-06-manifest-set-live-bypass): the
        # manifest is content, and content reaches live ONLY through
        # playbook_publish (gates + the owner card). Until 0.56.0 this tool
        # minted a version and flipped `live_version` itself — the side door
        # that put v60 live over a v59 candidate awaiting approval
        # (2026-09-05). Now it saves a CANDIDATE through the same path as
        # playbook_edit and never touches live.
        author = writer_identity()  # phase 11: `agent` or `delegation:<id>`
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name).with_for_update()
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            # phase 11 guard, as in playbook_edit: another author's
            # unpublished candidate is never replaced silently.
            conflict = await candidate_conflict(session, playbook, author)
            if conflict is not None:
                return json.dumps({
                    "saved": False,
                    "error": conflict_message(name, conflict),
                    "conflict": conflict,
                })
            # Operator decision P4-5 (plan 033): a pending candidate by the
            # same author is MERGED — the new manifest is applied on top of
            # that candidate's definition/code/format and becomes the single
            # candidate (the previous candidate row stays in history), so a
            # code change and a manifest change publish together. No pending
            # candidate → the candidate is the live content + new manifest.
            base_row = None
            if playbook.candidate_version:
                base_row = await _get_version_row(
                    session, playbook, playbook.candidate_version,
                )
            had_live = await _ensure_live_row(session, playbook) is not None
            if base_row is not None:
                definition, code, fmt = (
                    base_row.definition, base_row.code, _row_format(base_row),
                )
                message = "manifest updated on candidate"
            else:
                definition, code, fmt = (
                    playbook.definition, playbook.code,
                    getattr(playbook, "format", "pblang") or "pblang",
                )
                message = "manifest updated"
            await mint_version(
                session, playbook,
                definition=definition, code=code,
                manifest=manifest, author=author,
                message=message + (f": {why}" if why else ""),
                format=fmt,
            )
            playbook.candidate_version = playbook.version
            if not had_live:
                # never published: the row mirrors the candidate so reads
                # (GET /playbooks/{name}, the edit read stage) show it —
                # the same mirror playbook_edit keeps for code.
                playbook.manifest = manifest
            await session.commit()
            new_version = playbook.version
            live_version = _live_version_of(playbook)
        await events.emit("playbook.candidate.saved", {
            "name": name, "candidate_version": new_version,
        })
        if live_version is None:
            next_text = (
                "No live version yet — test the candidate with "
                "playbook_run_candidate, then playbook_publish(name) makes "
                "it live."
            )
        else:
            next_text = (
                "The LIVE playbook is unchanged — triggers and playbook_run "
                f"still execute version {live_version} with its current "
                "manifest. Test the candidate with playbook_run_candidate "
                "(the publish gate wants a green run of this exact version), "
                "then call playbook_publish(name) to make it live."
            )
        return json.dumps({
            "playbook": name,
            "version": new_version,
            "candidate_version": new_version,
            "live_version": live_version,
            "status": "manifest_candidate_saved",
            "manifest_chars": len(manifest),
            "note": (
                f"manifest saved as candidate v{new_version} — publish to "
                "go live"
            ),
            "next": f"{next_text} {_overview_hint(name)}",
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
                "whenever the playbook's intent drifts from what it says. "
                "Saves a CANDIDATE (merged onto the pending candidate if you "
                "have one) — the live playbook is unchanged until "
                "playbook_publish; nothing goes live from this call."
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
    ) -> str | None:
        """plans/018 phase 1: ONE owner approval for the whole change, raised
        AFTER every gate passed — owner-language presentation (luna 094) up
        front, the technical diff collapsed behind it. Returns a refusal JSON
        when the owner rejected, None to proceed. Contexts without an
        approval engine (unit tests, headless cores) proceed ungated — the
        old prompt_always card did not exist there either.

        021: the card carries ✓/✗ status bullets built from the gates list,
        so the owner sees the picture (test run done? tools reachable?)
        before deciding. Every agent publish raises the card — no standing
        skip.
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
        # plans/030: wake-on-decision. On cores with request_nowait (luna
        # plans/103) the card is raised WITHOUT parking this handler under the
        # ToolDef timeout — the tool returns "awaiting the owner" and the
        # engine's orphan-resume wake continues the conversation when the
        # owner decides (the re-issued publish auto-approves against the
        # short-TTL pre-grant). The wake targets the conversation this turn
        # runs in; ops is the headless fallback. Old cores keep the parked
        # request() contract (hence timeout_seconds=900 stays on the tools).
        wake_conv = (
            getattr(ctx, "current_conversation_id", None)
            or await ops_conversation_id(ctx)
        )
        nowait = getattr(approvals, "request_nowait", None)
        try:
            requester = nowait if nowait is not None else approvals.request
            decision = await requester(
                kind="playbook_change",
                summary=summary,
                payload=payload,
                requested_by_plugin="plugin-playbooks",
                risk_level="medium",
                conversation_id=wake_conv,
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
        if getattr(decision, "decision", None) == "pending":
            # plans/030: nothing published yet — the owner has the card. Do
            # NOT phrase this as a failure the agent should work around.
            return json.dumps({
                "status": "awaiting_owner_approval",
                "error": (
                    f"Not published yet — the {action} of '{name}' version "
                    f"{target_version} is awaiting the owner's approval."
                ),
                "approval_id": str(getattr(decision, "request_id", "")),
                "hint": (
                    "You will be WOKEN automatically when the owner decides "
                    "— do NOT retry this call and do NOT poll for the "
                    "decision. Finish anything else you were doing, tell "
                    "the owner the change awaits their approval, and end "
                    "your turn. If woken with an approval, re-issue this "
                    "exact call — it is pre-approved and will execute."
                ),
            })
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
            # plans/032 phase 04: python rows go through the checker.
            if (row.definition or {}).get("format") == "python":
                _, errors, _ = _python_check(
                    row.code or "", name=name, version=row.version,
                    inputs_schema=(row.definition or {}).get("inputs"),
                    registry=getattr(runner, "_tools", None),
                )
            else:
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
            # gate 2 (0.26.0, 089 contract #8): the TEST-RUN gate — a green
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
            # gate 3 (0.12.0): probes — every tool the version touches must
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
            # outside any transaction. Probe caches commit here.
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
            # plans/033: a candidate row carries its own manifest (a
            # manifest change is a candidate now) — the card shows the
            # diff, and the flip below applies it. A row without one keeps
            # the live manifest (_apply_version_to_live never NULLs it).
            manifest_after = (row.manifest or "") or manifest_before
            await session.commit()

        refusal = await _request_publish_decision(
            name=playbook_name, action=action,
            target_version=target_version, explanation=explanation,
            evidence=evidence_ref, failed_run=failed_run_ref, gates=gates,
            before_code=before_code, after_code=after_code,
            manifest_before=manifest_before, manifest_after=manifest_after,
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
            # plans/033: the version row's manifest goes live with its
            # content — for a candidate (playbook_manifest_set saves one)
            # and for a restore alike. A row with no manifest keeps live's.
            _apply_version_to_live(playbook, row, restore_manifest=True)
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
                # plans/032 phase 04: the first publish has nothing to
                # roll back to.
                f"Version {new_live} is the FIRST live version — triggers "
                "and playbook_run execute it from now on."
                if old_live is None else
                f"Version {new_live} is now LIVE — triggers and "
                f"playbook_run execute it. playbook_rollback(name) "
                f"restores version {old_live} if it misbehaves."
            ),
            # plans/032 phase 09
            "next": _overview_hint(name),
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
                "validation, a GREEN TEST RUN of that exact version "
                "since its last edit (playbook_run_candidate provides it — "
                "run the test BEFORE publishing), and tool probes. After "
                "the gates pass, the owner gets ONE approval card for the "
                "whole change: ✓/✗ check bullets and your `explanation` in "
                "plain language up front, the technical diff collapsed "
                "behind it. Every publish is announced in the ops chat "
                "with its evidence. " + PUBLISH_RULE
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
            if live_n is None:
                # plans/032 phase 04: candidate-only — nothing to restore.
                return json.dumps({
                    "error": f"'{name}' has no live version to roll back "
                             "from — publish the candidate first.",
                })
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
        try:
            run = await runner.start_run_background(
                shim, inputs=input_data, trigger="agent-candidate", is_test=True,
            )
        except InputTypeError as e:
            # plans/032 phase 04: loud intake — same shape as playbook_run.
            return json.dumps({
                "status": "rejected", "error": str(e),
                "input": e.input, "expected": e.expected,
                "candidate_version": candidate_version,
            })
        waited = await runner.wait_for_run(run.id, timeout=wait_seconds)
        status = waited.status if waited else run.status

        # plans/032 phase 09: the envelope — the row's stamp is the
        # candidate number (the shim carries it as live_version).
        env = _envelope(
            "candidate_test_run", side_effects=True,
            version=getattr(run, "playbook_version", candidate_version),
            version_role="candidate", run_id=str(run.id),
        )
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
        if status == "parked":
            # plans/032 phase 07: the candidate run parked (approval / event)
            result["parked_on"] = getattr(waited, "parked_on", None)
            result["message"] = (
                f"{_parked_message(result['parked_on'], False)} {_overview_hint(name)}"
            )
        elif status == "running":
            result["message"] = (
                "Still executing in the background — poll "
                "playbook_status(run_id) until 'done'/'failed'. Do NOT "
                f"re-run, do NOT report results yet. {_overview_hint(name)}"
            )
        elif status == "failed":
            fabricate = (
                "Candidate test run FAILED. Do NOT fabricate results — "
                "playbook_publish will refuse until a green test run "
                "exists. Check playbook_status for the error details."
            )
            # plans/032 phase 04 (docs/v2.md §7): the one-liner is readable
            # here, same as playbook_run.
            async with session_factory() as session:
                row_run = await session.get(PlaybookRun, run.id)
            run_error = getattr(row_run, "error", None) if row_run is not None else None
            if getattr(shim, "format", "pblang") == "python" and run_error:
                result["error"] = f"{run_error} {fabricate}"
            else:
                result["error"] = fabricate
                if run_error:
                    result["error_detail"] = run_error
            result["error_type"] = getattr(row_run, "error_type", None) if row_run is not None else None
            failed_at = getattr(row_run, "failed_at", None) if row_run is not None else None
            result["failed_at"] = failed_at.isoformat() if failed_at else None
        elif status == "done":
            async with session_factory() as session:
                steps = (await session.execute(
                    select(PlaybookStepRun).where(PlaybookStepRun.run_id == run.id)
                )).scalars().all()
                row_run = await session.get(PlaybookRun, run.id)
                result["step_results"] = {
                    s.step_id: s.outputs for s in steps if s.outputs
                }
                # plans/032 phase 08: what `run()` returned
                result["result"] = getattr(row_run, "result", None) if row_run is not None else None
        result["next"] = _overview_hint(name)
        return json.dumps(_with_envelope(env, result))

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
                "playbook stays untouched. A done run reports step_results "
                "and `result` (what a python playbook's run() returned). "
                "Prefer playbook_dry_run first; use this when the owner "
                "wants proof against real systems before playbook_publish. "
                "The result opens with kind / side_effects / version / "
                "version_role / run_id — quote kind and version when you "
                "report it."
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

    # --- playbook_preflight (0.12.0, plans/002 phase 5) ---
    async def _preflight(*, name: str, version: str = "auto") -> str:
        async with session_factory() as session:
            playbook = (await session.execute(
                select(Playbook).where(Playbook.name == name)
            )).scalar_one_or_none()
            if not playbook:
                return json.dumps({"error": f"Playbook '{name}' not found"})
            resolved = await _resolve_target(session, playbook, version)
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
                f"playbook to stop using the tool. {_overview_hint(name)}"
            )
        elif summary["ok"] == 0:
            result["note"] = (
                "No tool declares a probe yet — nothing verified, nothing "
                "known-broken. This is normal today; probes arrive per-plugin."
            )
        if "next" not in result:
            result["next"] = _overview_hint(name)  # plans/032 phase 09
        return json.dumps(result)

    tools.append((
        ToolDef(
            name="playbook_preflight",
            modes=["planning", "building"],
            description=(
                "Check that every tool a playbook touches would work RIGHT "
                "NOW (credentials alive, resources reachable) — the check "
                "a dry run can't do because it stubs the outside world. Probes "
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
