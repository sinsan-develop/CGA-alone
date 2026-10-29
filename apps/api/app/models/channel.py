from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Uuid, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ChannelRoom(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "channel_rooms"
    __table_args__ = (
        UniqueConstraint("channel_type", "client_room_id", name="uq_channel_rooms_channel_client_room"),
    )

    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    client_room_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    bot_version_id: Mapped[UUID] = mapped_column(ForeignKey("bot_versions.id"), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(120), nullable=False, default="visitor")
    participant_name: Mapped[str] = mapped_column(String(120), nullable=False, default="사용자")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ChannelMessage(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "channel_messages"

    room_id: Mapped[UUID] = mapped_column(ForeignKey("channel_rooms.id"), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(120), nullable=False)
    participant_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    participant_name: Mapped[str] = mapped_column(String(120), nullable=False)
    message_type: Mapped[str] = mapped_column(String(40), nullable=False, default="text")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ChannelQueueEvent(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "channel_queue_events"
    __table_args__ = (Index("uq_queue_persistent_message", "binding_id", "client_message_key", unique=True,
                           postgresql_where=text("operation_kind = 'persistent_llm' AND client_message_key IS NOT NULL")),)

    operation_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="channel", server_default="channel")
    binding_id: Mapped[UUID | None] = mapped_column(ForeignKey("channel_llm_bindings.id", name="fk_queue_llm_binding"), nullable=True)
    client_message_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phase: Mapped[str] = mapped_column(String(40), nullable=False, default="queued", server_default="queued")
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_response_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    room_id: Mapped[UUID | None] = mapped_column(ForeignKey("channel_rooms.id"), nullable=True)
    request_message_id: Mapped[UUID | None] = mapped_column(ForeignKey("channel_messages.id"), nullable=True)
    channel_type: Mapped[str] = mapped_column(String(40), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    bot_version_id: Mapped[UUID] = mapped_column(ForeignKey("bot_versions.id"), nullable=False)
    participant_id: Mapped[str] = mapped_column(String(120), nullable=False, default="visitor")
    intent_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sender_system: Mapped[str] = mapped_column(String(80), nullable=False, default="external-channel")
    receiver: Mapped[str] = mapped_column(String(120), nullable=False, default="Aidot")
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    receive_status: Mapped[str] = mapped_column(String(40), nullable=False, default="received")
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="queued")
    parameter_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ChannelLlmBinding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """기존 방과 인증 주체에 귀속된 provider 문맥 매핑."""
    __tablename__ = "channel_llm_bindings"
    __table_args__ = (
        UniqueConstraint("room_id", "principal_key", "generation", name="uq_llm_binding_generation"),
        UniqueConstraint("provider_config_ref", "provider_conversation_id", name="uq_llm_provider_conversation"),
        CheckConstraint("generation > 0", name="ck_llm_binding_generation"),
        Index("uq_llm_binding_live", "room_id", "principal_key", unique=True, postgresql_where=text("status <> 'closed'")),
    )
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("groups.id"), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("bot_versions.id"), nullable=False)
    room_id: Mapped[UUID] = mapped_column(ForeignKey("channel_rooms.id"), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    principal_key: Mapped[str] = mapped_column(String(160), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="open", server_default="open")
    provider_config_ref: Mapped[str] = mapped_column(String(160), nullable=False, default="", server_default="")
    provider_conversation_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_state: Mapped[str] = mapped_column(String(40), nullable=False, default="uninitialized", server_default="uninitialized")
    snapshot_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    committed_turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    memory_prefix_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChannelLlmVersionGuard(TimestampMixin, Base):
    __tablename__ = "channel_llm_version_guards"
    version_id: Mapped[UUID] = mapped_column(ForeignKey("bot_versions.id"), primary_key=True)
    mutation_status: Mapped[str] = mapped_column(String(40), nullable=False, default="idle", server_default="idle")
    mutation_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    operation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class LlmBudgetAccount(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "llm_budget_accounts"
    __table_args__ = (UniqueConstraint("scope_kind", "scope_key", "currency", name="uq_llm_budget_scope"),
        CheckConstraint("limit_amount >= 0 AND limit_count >= 0 AND spent_amount >= 0 AND reserved_amount >= 0 AND spent_count >= 0 AND reserved_count >= 0",name="ck_llm_budget_nonnegative"),)
    scope_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(200), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    tariff_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    unit_scale: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1000000, server_default="1000000")
    limit_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spent_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    spent_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reserved_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    blocked_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)


class LlmBudgetReservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "llm_budget_reservations"
    __table_args__ = (UniqueConstraint("event_id", "account_id",name="uq_llm_budget_event_account"),
                     CheckConstraint("amount_upper >= 0 AND (actual_amount IS NULL OR actual_amount >= 0)",name="ck_llm_reservation_nonnegative"),)
    reservation_group_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("channel_queue_events.id"), nullable=False)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("llm_budget_accounts.id"), nullable=False)
    quote_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_upper: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="reserved", server_default="reserved")
    receipt_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    usage_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
