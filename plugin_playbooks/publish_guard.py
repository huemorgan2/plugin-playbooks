"""plans/034 (0.57.0; luna-fixer plans/2026-09-06-playbook-publish-verify)
— publish read-back verification and the approve→wake→re-gate loop guard.

Production, 2026-09-05 (vaselin-scanny-2): the agent narrated "v59 is live"
and "v61 is live" while `live_version` read 60, and one publish minted three
approval cards because the woken re-issue re-gated every time (core grant
hole, luna plan 108). Two plugin-side defences live here:

1. `read_back_live_version` — after the flip commits, `_do_publish` reads
   the stored row again in a FRESH session and reports THAT number as the
   verified truth (or an explicit error when it disagrees).
2. The loop guard — the plugin remembers the last card it raised per
   playbook on the `playbooks` row (`last_card_*` columns) and learns the
   owner's decision from `approval.decided`. A re-issue for the exact same
   (playbook, action, version) inside `REISSUE_WINDOW` (operator decision
   P4-6: 30 minutes) is examined BEFORE any card is minted:

   - still pending  → the same awaiting result, same approval_id, no card;
   - rejected/expired → the guard clears and the normal flow runs;
   - approved (engine `get()` when available, else the recorded decision)
     or undeterminable → the exact-payload grant is looked up WITHOUT
     minting; a hit proceeds (the engine auto-approves inline, audited),
     anything else — no grant, or an engine that cannot say — is
     `BROKEN_FLOW_ERROR`: fail loud, no retry hint, no second card.

Relative imports only (loader-style import test).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from .models import Playbook
from .versioning import live_version_of

log = logging.getLogger("luna.playbooks.publish_guard")

# operator decision P4-6
REISSUE_WINDOW = timedelta(minutes=30)

APPROVAL_KIND = "playbook_change"

BROKEN_FLOW_STATUS = "approval_flow_broken"
BROKEN_FLOW_ERROR = (
    "the approval system approved this but the re-issued call re-gated — "
    "the approval flow is broken; stop and tell the owner"
)

_TERMINAL_NO = frozenset({"rejected", "expired", "cancelled", "superseded"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts: datetime | None) -> datetime | None:
    """SQLite hands naive datetimes back for `DateTime(timezone=True)`."""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


async def _load(session, name: str) -> Playbook | None:
    return (await session.execute(
        select(Playbook).where(Playbook.name == name)
    )).scalar_one_or_none()


# ------------------------------------------------------------- read-back

async def read_back_live_version(session_factory, name: str) -> int | None:
    """The stored live version of `name`, read in a fresh session (never the
    object the caller just mutated). None when the row is missing or has no
    live version. Monkeypatch point for the "mocked store" test."""
    async with session_factory() as session:
        playbook = await _load(session, name)
        if playbook is None:
            return None
        return live_version_of(playbook)


# --------------------------------------------------------------- memory

async def remember_card(
    session_factory, *, name: str, action: str, version: int,
    approval_id: str, now: datetime | None = None,
) -> None:
    """A card was raised (pending): remember it on the playbook row and
    forget any earlier decision."""
    async with session_factory() as session:
        playbook = await _load(session, name)
        if playbook is None:
            return
        playbook.last_card_action = action
        playbook.last_card_version = int(version)
        playbook.last_card_approval_id = str(approval_id)[:64]
        playbook.last_card_raised_at = now or _now()
        playbook.last_card_decision = None
        playbook.last_card_decided_at = None
        await session.commit()


async def note_decision(
    session_factory, *, approval_id: str, decision: str,
    now: datetime | None = None,
) -> bool:
    """`approval.decided` for a remembered card: record the decision. True
    when a playbook row carried that approval id."""
    approval_id = str(approval_id)
    async with session_factory() as session:
        playbook = (await session.execute(
            select(Playbook).where(Playbook.last_card_approval_id == approval_id)
        )).scalar_one_or_none()
        if playbook is None:
            return False
        playbook.last_card_decision = str(decision)[:16]
        playbook.last_card_decided_at = now or _now()
        await session.commit()
        return True


async def clear_card(session_factory, name: str) -> None:
    async with session_factory() as session:
        playbook = await _load(session, name)
        if playbook is None:
            return
        playbook.last_card_action = None
        playbook.last_card_version = None
        playbook.last_card_approval_id = None
        playbook.last_card_raised_at = None
        playbook.last_card_decision = None
        playbook.last_card_decided_at = None
        await session.commit()


# ---------------------------------------------------------------- guard

async def _engine_status(approvals: Any, approval_id: str) -> str | None:
    """The engine's view of the card, or None when it cannot say."""
    getter = getattr(approvals, "get", None)
    if not callable(getter):
        return None
    try:
        import uuid as _uuid
        try:
            key: Any = _uuid.UUID(str(approval_id))
        except ValueError:
            key = approval_id
        req = await getter(key)
    except Exception as e:  # noqa: BLE001 — the guard never crashes a publish
        log.warning("publish_guard: approvals.get(%s) failed: %s", approval_id, e)
        return None
    if req is None:
        return None
    status = getattr(req, "status", None)
    return str(status) if status else None


async def _grant_hit(approvals: Any, payload: dict[str, Any]) -> bool | None:
    """True/False for a grants lookup, None when the engine exposes none."""
    grants = getattr(approvals, "grants", None)
    lookup = getattr(grants, "lookup_detail_full", None) if grants is not None else None
    if not callable(lookup):
        return None
    try:
        detail = await lookup(APPROVAL_KIND, "", payload, plugin=None)
    except Exception as e:  # noqa: BLE001
        log.warning("publish_guard: grants lookup failed: %s", e)
        return None
    if detail is None:
        return False
    decision = detail[0] if isinstance(detail, (tuple, list)) and detail else detail
    return str(decision) == "approved"


async def check_reissue(
    session_factory, approvals: Any, *, name: str, action: str, version: int,
    payload: dict[str, Any], now: datetime | None = None,
) -> dict[str, Any] | None:
    """Look at the remembered card BEFORE minting one for `payload`.

    Returns None to proceed with the normal flow (raise / auto-approve via
    the engine), or a verdict dict:
      {"kind": "awaiting", "approval_id": ...}  — the card is still open;
      {"kind": "broken",   "approval_id": ...}  — approved, yet re-gated.
    """
    now = now or _now()
    async with session_factory() as session:
        playbook = await _load(session, name)
        if playbook is None:
            return None
        remembered = (
            playbook.last_card_approval_id,
            playbook.last_card_action,
            playbook.last_card_version,
            _aware(playbook.last_card_raised_at),
            playbook.last_card_decision,
            _aware(playbook.last_card_decided_at),
        )
    approval_id, last_action, last_version, raised_at, decision, decided_at = remembered
    if not approval_id or last_action != action or last_version != int(version):
        return None  # a different change — never blocked
    anchor = decided_at or raised_at
    if anchor is None or now - anchor > REISSUE_WINDOW:
        return None  # stale memory: the window has passed

    status = await _engine_status(approvals, approval_id) or decision
    if status == "pending":
        return {"kind": "awaiting", "approval_id": approval_id}
    if status in _TERMINAL_NO:
        await clear_card(session_factory, name)
        return None
    # approved, or nobody can say: the woken re-issue is legitimate ONLY if
    # the exact-payload pre-grant is there for the engine to auto-approve.
    hit = await _grant_hit(approvals, payload)
    if hit is True:
        return None
    # No grant for a card that is not known to be open: on a real core the
    # engine dedups a still-pending same-payload card, so a fresh card here
    # can only mean the owner's decision did not reach this call — the
    # production loop. An engine that can say nothing (no get(), no grants)
    # gets the same verdict: the plan fails loud, never silent.
    log.error(
        "publish_guard: %s of '%s' v%s was approved (card %s) but the "
        "re-issue re-gated — refusing to mint another card",
        action, name, version, approval_id,
    )
    return {"kind": "broken", "approval_id": approval_id}


def broken_flow_result(
    *, name: str, action: str, version: int, approval_id: str,
) -> dict[str, Any]:
    """The hard error the agent gets: no `status: awaiting`, no retry hint."""
    return {
        "status": BROKEN_FLOW_STATUS,
        "error": BROKEN_FLOW_ERROR,
        "playbook": name,
        "action": action,
        "version": int(version),
        "approval_id": str(approval_id),
        "hint": (
            "Do NOT retry and do NOT re-issue this publish. Nothing was "
            f"published; the owner's approval for version {version} did not "
            "reach the re-issued call. Tell the owner exactly this and end "
            "your turn."
        ),
    }
