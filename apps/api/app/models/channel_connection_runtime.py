from __future__ import annotations
from datetime import datetime
from uuid import UUID
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ChannelInstallationLease(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "channel_installation_leases"
    __table_args__ = (UniqueConstraint("organization_id","provider","listener_scope_hash",name="uq_connection_listener_scope"),)
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"),nullable=False)
    provider: Mapped[str] = mapped_column(String(40),nullable=False)
    installation_id: Mapped[str] = mapped_column(String(200),nullable=False)
    account_id: Mapped[str] = mapped_column(String(200),nullable=False)
    listener_scope_hash: Mapped[str] = mapped_column(String(64),nullable=False)
    owner: Mapped[str | None] = mapped_column(String(120))
    fence: Mapped[int] = mapped_column(BigInteger,nullable=False,default=0,server_default="0")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor_json: Mapped[dict] = mapped_column(JSONB,nullable=False,default=dict,server_default=text("'{}'::jsonb"))


class ChannelConnectionRoom(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """외부 범위를 기존 방에 연결한다. 대화 내용/버전은 기존 ChannelRoom이 소유한다."""
    __tablename__ = "channel_connection_rooms"
    __table_args__ = (UniqueConstraint("connection_id","native_room_hash",name="uq_connection_native_room"),UniqueConstraint("room_id",name="uq_connected_existing_room"))
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("channel_connections.id"),nullable=False)
    native_room_hash: Mapped[str] = mapped_column(String(64),nullable=False)
    room_id: Mapped[UUID] = mapped_column(ForeignKey("channel_rooms.id"),nullable=False)
    participant_ciphertext: Mapped[str | None] = mapped_column(Text)
    credential_key_version: Mapped[str | None] = mapped_column(String(80))


class ChannelConnectionDelivery(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "channel_connection_deliveries"
    __table_args__ = (
        UniqueConstraint("connection_id","event_key",name="uq_connection_delivery_event"),
        Index("ix_connection_delivery_pending","connection_id","status","created_at"),
        CheckConstraint("status IN ('received','executing','ready','sending','sent','failed','unknown','cancelled')",name="ck_connection_delivery_status"),
    )
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"),nullable=False)
    connection_id: Mapped[UUID] = mapped_column(ForeignKey("channel_connections.id"),nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger,nullable=False)
    installation_fence: Mapped[int] = mapped_column(BigInteger,nullable=False)
    native_room_hash: Mapped[str] = mapped_column(String(64),nullable=False)
    native_actor_hash: Mapped[str] = mapped_column(String(64),nullable=False)
    event_key: Mapped[str] = mapped_column(String(64),nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64),nullable=False)
    status: Mapped[str] = mapped_column(String(20),nullable=False,default="received",server_default="received")
    event_json: Mapped[dict] = mapped_column(JSONB,nullable=False)
    result_json: Mapped[dict] = mapped_column(JSONB,nullable=False,default=dict,server_default=text("'{}'::jsonb"))
    context_ciphertext: Mapped[str | None] = mapped_column(Text)
    context_key_version: Mapped[str | None] = mapped_column(String(80))
    queue_event_id: Mapped[UUID | None] = mapped_column(ForeignKey("channel_queue_events.id"),nullable=True)
    execution_claim_id: Mapped[str | None] = mapped_column(String(36),nullable=True)
    send_claim_id: Mapped[str | None] = mapped_column(String(36),nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_ids: Mapped[list] = mapped_column(JSONB,nullable=False,default=list,server_default=text("'[]'::jsonb"))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    room_id: Mapped[UUID | None] = mapped_column(ForeignKey("channel_rooms.id"))
    request_message_id: Mapped[UUID | None] = mapped_column(ForeignKey("channel_messages.id"))
