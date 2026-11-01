from __future__ import annotations
from datetime import datetime
from uuid import UUID
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ChannelConnection(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "channel_connections"
    __table_args__ = (
        Index("uq_channel_connection_target", "organization_id", "provider", "installation_id", "account_id", "target_id", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_channel_connections_parent", "channel_id", "deleted_at"),
        CheckConstraint("generation > 0", name="ck_connection_generation"),
        CheckConstraint("desired_state IN ('connected','disconnected')", name="ck_connection_desired"),
        CheckConstraint("observed_state IN ('disconnected','connecting','connected','disconnecting','error')", name="ck_connection_observed"),
    )
    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    channel_id: Mapped[UUID] = mapped_column(ForeignKey("admin_channels.id"), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    installation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    account_id: Mapped[str] = mapped_column(String(200), nullable=False)
    target_id: Mapped[str] = mapped_column(String(200), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(20), nullable=False, default="disconnected", server_default="disconnected")
    observed_state: Mapped[str] = mapped_column(String(20), nullable=False, default="disconnected", server_default="disconnected")
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    credential_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_key_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    listener_scope_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accept_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
