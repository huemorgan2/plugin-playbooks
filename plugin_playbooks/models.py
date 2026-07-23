"""SQLAlchemy models for the Playbooks engine.

Plugin-owned tables — all additive, no changes to existing schema.

009.001/phase03 (E4): bound to the plugin's OWN declarative base, not core's
``luna.data.models.Base``. Table names/columns are byte-identical to the
pre-split schema (existing rows must keep loading); creation happens in
``on_load`` via ``ctx.engine`` with ``checkfirst=True``.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
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
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="enabled", nullable=False)
    agent_autonomy: Mapped[str] = mapped_column(String(32), default="agent_must_confirm", nullable=False)
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
    author: Mapped[str] = mapped_column(String(64), default="owner", nullable=False)
    message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    promoted_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_edit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
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
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
