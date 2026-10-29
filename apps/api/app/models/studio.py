from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Bot(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "bots"
    __table_args__ = (
        Index("idx_bots_org_group_deleted_updated", "organization_id", "group_id", "deleted_at", "updated_at"),
        Index("uq_bots_live_creator", "created_by", unique=True,
              postgresql_where=text("created_by IS NOT NULL AND deleted_at IS NULL")),
    )

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("groups.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    data_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    active_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bot_versions.id"),
        nullable=True,
    )
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class BotHub(TimestampMixin, Base):
    __tablename__ = "bot_hubs"

    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), primary_key=True)
    call_method: Mapped[str] = mapped_column(String(20), nullable=False, default="button")
    button_match_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="exact")
    greeting_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent_cutoff_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)
    similar_intent_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.85)
    max_intent_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    show_members_in_greeting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unrecognized_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    multiple_candidates_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_in_progress_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    apply_timeout_to_push: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timeout_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    no_bot_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    no_bot_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class BotHubMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bot_hub_members"
    __table_args__ = (
        UniqueConstraint("hub_id", "bot_id", name="uq_bot_hub_members_hub_bot"),
        Index("idx_bot_hub_members_hub_sort", "hub_id", "sort_order"),
    )

    hub_id: Mapped[UUID] = mapped_column(ForeignKey("bot_hubs.bot_id"), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    use_as_small_talk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

class BotVersion(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "bot_versions"
    __table_args__ = (
        UniqueConstraint("bot_id", "version_no", name="uq_bot_versions_bot_id_version_no"),
    )

    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    asset_counts_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    scenario_validation_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    nlu_training_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    entities_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    dictionary_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    apis_json: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    system_config_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    copied_from_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("bot_versions.id"),
        nullable=True,
    )
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class EditLock(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "edit_locks"
    __table_args__ = (
        UniqueConstraint("bot_id", "version_id", "dialog_id", "area", name="uq_edit_locks_target"),
    )

    organization_id: Mapped[UUID] = mapped_column(ForeignKey("organizations.id"), nullable=False)
    group_id: Mapped[UUID] = mapped_column(ForeignKey("groups.id"), nullable=False)
    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("bot_versions.id"), nullable=False)
    dialog_id: Mapped[str] = mapped_column(String(120), nullable=False)
    area: Mapped[str] = mapped_column(String(40), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    owner_login_id: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VersionDialogAsset(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "version_dialog_assets"
    __table_args__ = (
        Index(
            "uq_version_dialog_assets_active_dialog",
            "version_id",
            "dialog_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_version_dialog_assets_version_sort", "version_id", "sort_order"),
        Index("idx_version_dialog_assets_version_kind", "version_id", "kind"),
    )

    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("bot_versions.id"), nullable=False)
    dialog_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class VersionDialogFlowGraph(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "version_dialog_flow_graphs"
    __table_args__ = (
        Index(
            "uq_version_dialog_flow_graphs_active_dialog",
            "version_id",
            "dialog_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_version_dialog_flow_graphs_version", "version_id"),
    )

    bot_id: Mapped[UUID] = mapped_column(ForeignKey("bots.id"), nullable=False)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("bot_versions.id"), nullable=False)
    dialog_id: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
