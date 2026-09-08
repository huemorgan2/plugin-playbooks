"""SQLAlchemy models for the Playbooks engine.

Plugin-owned tables — all additive, no changes to existing schema.

009.001/phase03 (E4): bound to the plugin's OWN declarative base, not core's
``luna.data.models.Base``. Table names/columns are byte-identical to the
pre-split schema (existing rows must keep loading); creation happens in
``on_load`` via ``ctx.engine`` with ``checkfirst=True``.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from luna_sdk import JSONB, UUID, declarative_base

Base = declarative_base()


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Playbook(Base):
    __tablename__ = "playbooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    when_to_use: Mapped[str] = mapped_column(Text, default="", nullable=False)
    inputs_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 0.8.0 (plans/002 phase 1): the pblang Python source this definition was
    # compiled from. NULL means "derive via codegen on read" — any write path
    # that changes `definition` without code MUST null this out (stale code is
    # worse than no code).
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # plans/032 phase 04: the playbook's language — "pblang" (the v1 DSL,
    # `definition` is a compiled PlaybookDef) or "python" (a v2
    # `async def run(ctx, inputs)` playbook, `definition` is the checker
    # summary). Existing rows read pblang from the DDL default.
    format: Mapped[str] = mapped_column(String(16), default="pblang", nullable=False)
    # 0.9.0 (plans/002 phase 2): free-text intent manifest (markdown). Empty
    # string = no manifest yet; the drift gate only engages when non-empty.
    manifest: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 0.10.0 (plans/002 phase 3): `version` is the monotonic counter (highest
    # version number ever created). `live_version` is what triggers/runs
    # execute — playbook.definition/code/manifest always hold ITS content.
    # 0 means "same as version" (pre-0.10 rows; backfilled on load) — unless
    # the row carries `candidate_version`, where 0 means "no live version"
    # (plans/032 phase 04: propose saves a candidate; publish makes it live).
    live_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # the one un-promoted candidate (its content lives in playbook_versions);
    # NULL = no candidate. A new save overwrites the pointer, not the history.
    candidate_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="enabled", nullable=False)
    # 0.21.0 (plans/014): failure-digest ack, scoped per version. When it
    # equals the effective live version the owner has decided about that
    # version's failures and the prompt digest stays silent; any publish
    # (new live version) re-arms it with no write here. NULL = never acked.
    failures_acked_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_autonomy: Mapped[str] = mapped_column(String(32), default="agent_must_confirm", nullable=False)
    # 0.26.0 (plans/015, 089 §3): legacy. 'ask' (default) | 'auto'. The ops
    # mode that honored 'auto' was removed (luna 098); agent publishing is
    # governed by the machine gates + one approval card (021).
    # Kept only so old rows keep loading.
    publish_autonomy: Mapped[str] = mapped_column(String(16), default="ask", nullable=False)
    # 0.28.0 (plans/016 phase 6): owner-switchable publish gate (Settings →
    # Publish). Off = the gate still runs and is reported, but never refuses.
    publish_require_run: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), default="owner", nullable=False)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(), nullable=True)
    cost_estimate_cents: Mapped[float | None] = mapped_column(nullable=True)
    duration_estimate_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class PlaybookVersion(Base):
    __tablename__ = "playbook_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("playbooks.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 0.8.0: pblang source at snapshot time (NULL = derive via codegen).
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 0.9.0: manifest at snapshot time — intent history travels with versions.
    manifest: Mapped[str] = mapped_column(Text, default="", nullable=False)
    author: Mapped[str] = mapped_column(String(64), default="owner", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    promoted_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_edit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PlaybookEditTicket(Base):
    """0.9.0 (plans/002 phase 2): staged-edit tickets.

    The write stage of playbook_edit requires a ticket issued by the read
    stage — the gate that forces "read the manifest + current code before you
    write". Single-use, 15-minute TTL; expired/used rows are swept
    convergently whenever a new ticket is issued.
    """
    __tablename__ = "playbook_edit_tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("playbooks.id", ondelete="CASCADE"), nullable=False
    )
    # the playbook version the read stage handed out — a write against a
    # playbook that changed since is refused (edit was authored on stale code).
    base_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PlaybookRun(Base):
    __tablename__ = "playbook_runs"

    # plans/001: the playbook list reads "last run" and "runs per day" from
    # this table on every load. Both are index-range scans over
    # (playbook_id, started_at) — never a scan of the run history.
    __table_args__ = (
        Index("ix_playbook_runs_playbook_started", "playbook_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("playbooks.id", ondelete="CASCADE"), nullable=False
    )
    playbook_version: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str | None] = mapped_column(String(128), nullable=True)
    inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(), nullable=True)
    # 006.712: the conversation this run originated from (null for
    # trigger/cron runs). agent_steps pin it so send_chat_message lands
    # in the right chat.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(), nullable=True)
    # 0.26.0 (plans/015, 089 §1): the conversation this run's chat output
    # DELIVERS to, stamped at creation — live runs → the ops chat, test runs →
    # their originating chat. NULL = unroutable (no ops chat on this core);
    # delivery then behaves as before 0.26. Never resolved at delivery time.
    report_to: Mapped[uuid.UUID | None] = mapped_column(UUID(), nullable=True)
    # 0.26.0 (plans/015, 089 §1): True for test runs of a draft/candidate
    # version. Test runs are excluded from the failure digest and production
    # stats, and are the evidence playbook_publish's test gate looks for.
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 0.44.0 (plans/028): the agent was told it will be WOKEN with the result
    # (playbook_run outlived its wait window, or fire-and-forget). Durable so
    # the promise survives a restart — the orphan sweep honors it.
    wake_on_complete: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # plans/032 phase 02 (docs/v2.md §7): the run-level error contract. `error`
    # is the one-liner (`line <n>: <source> → <type>: <msg> after effect <id>`),
    # `error_type` the exception class / host error kind, `traceback` the
    # playbook-frame-only traceback text, `failed_at` when it landed. Written
    # by _complete_run for v1 and v2 runs alike; read by phase 04.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # plans/032 phase 07 (docs/v2.md §6): what a `parked` run waits on —
    # {kind: approval|event, since, due_at, approval_id|event_name}; NULL otherwise.
    parked_on: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class PlaybookStepRun(Base):
    __tablename__ = "playbook_step_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("playbook_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(String(128), nullable=False)
    step_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    outputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_cents: Mapped[float | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PlaybookProbeResult(Base):
    """0.12.0 (plans/002 phase 5): cached preflight probe results, one row
    per (playbook, tool). status: ok | unprobeable | failed. Feeds the
    publish gate note, UI badges, and the daily re-probe's transition
    detection (a row flipping into `failed` triggers a chat alert)."""
    __tablename__ = "playbook_probe_results"

    __table_args__ = (
        Index("ix_playbook_probe_results_playbook_tool", "playbook_id", "tool",
              unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("playbooks.id", ondelete="CASCADE"), nullable=False
    )
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    probed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class PlaybookDelegation(Base):
    """0.25.0 (plans/013, reinstated by plans/020): a delegated authoring job
    — one focused background agent turn working on playbooks with its own
    context. `events` is the live feed the progress card polls (list of {ts,
    phase, kind, label, detail, ms}); `card_token` is the capability secret
    baked into the card HTML so the sandboxed iframe (which can send no
    credentials) may read THIS delegation's status and nothing else."""
    __tablename__ = "playbook_delegations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    playbook: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    card_token: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(), nullable=True)
    # the chat card row posted for this delegation — kept so a follow-up
    # phase can reference/replace it.
    card_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    events: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PlaybookFixProposal(Base):
    """0.26.0 (plans/015, 089 §4): dedupe ledger for production-failure fix
    proposals. One OPEN row per (playbook, failure signature); a repeated
    failure updates the row (count/last_run_id) instead of filing a second
    proposal. Card/approval plumbing keys off this row; on cores without an
    ops chat the rows still record failures for when one appears.
    """
    __tablename__ = "playbook_fix_proposals"

    __table_args__ = (
        Index("ix_playbook_fix_proposals_pb_sig", "playbook_id", "signature"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("playbooks.id", ondelete="CASCADE"), nullable=False
    )
    # sha1 over (playbook, failed step, normalized error head) — the failure's
    # identity across repeats.
    signature: Mapped[str] = mapped_column(String(40), nullable=False)
    # open | approved | dismissed | resolved
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    title: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    diagnosis: Mapped[str] = mapped_column(Text, default="", nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class PlaybookDraft(Base):
    """In-progress canvas drafts — persisted so page reloads don't lose work."""
    __tablename__ = "playbook_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    playbook_id: Mapped[uuid.UUID | None] = mapped_column(UUID(), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), default="agent", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class PlaybookWatch(Base):
    """0.46.0 (plans/029): one-shot 'wake me when this playbook's next run
    finishes' promise. `conversation_id` is stamped from the calling turn at
    watch time — never reconstructed at delivery. `consumed_at` is the
    exactly-once claim (UPDATE … WHERE consumed_at IS NULL)."""
    __tablename__ = "playbook_watches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(), primary_key=True, default=_uuid)
    playbook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("playbooks.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PlaybookJournal(Base):
    """0.51.0 (plans/032 phase 06; docs/v2.md §6): the durable v2 journal.
    One row per effect occurrence of a run, keyed `(run_id, seq)`; row 0
    (`kind="run"`) carries the entry-0 fields in `args` and is the v2 marker
    the sweep and the resume scan key on. Rows are written `in_flight`
    BEFORE the effect executes (write-ahead) and overwritten in place; the
    store never deletes a row — rows cascade with the run row.
    `args` holds raw `vault:` refs only, never a resolved secret."""
    __tablename__ = "playbook_journal"
    __table_args__ = (
        Index("ux_playbook_journal_idem", "idempotency_key", unique=True),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(), ForeignKey("playbook_runs.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    call_site_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurrence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # the literal tool name (`tool`), the target playbook (`subtask`), else NULL
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    args: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # in_flight | done | failed | failed_handled | timed_out_unknown | parked (phase 07)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    attempts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    transcript: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    child_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(), nullable=True)
    dry: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # host wall time of the whole row (docs/v2.md §6 `ms`)
    ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # plans/032 phase 07: mirror of `playbook_runs.parked_on` on the parking effect's row
    parked_on: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
