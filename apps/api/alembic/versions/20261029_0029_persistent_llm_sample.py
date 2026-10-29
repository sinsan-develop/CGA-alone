"""기존 방/큐를 유지하는 지속 대화 매핑·버전 guard·공용 예산 원장."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20261029_0029"
down_revision = "20261029_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_budget_accounts",
        sa.Column("scope_kind", sa.String(length=40), nullable=False),
        sa.Column("scope_key", sa.String(length=200), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("tariff_version", sa.String(length=80), nullable=True),
        sa.Column(
            "unit_scale", sa.BigInteger(), server_default="1000000", nullable=False
        ),
        sa.Column("limit_amount", sa.BigInteger(), nullable=False),
        sa.Column("limit_count", sa.BigInteger(), nullable=False),
        sa.Column("spent_amount", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "reserved_amount", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("spent_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "reserved_count", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column(
            "blocked", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("blocked_reason", sa.String(length=200), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "limit_amount >= 0 AND limit_count >= 0 AND spent_amount >= 0 AND reserved_amount >= 0 AND spent_count >= 0 AND reserved_count >= 0",
            name="ck_llm_budget_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_kind", "scope_key", "currency", name="uq_llm_budget_scope"
        ),
    )
    op.create_table(
        "channel_llm_version_guards",
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column(
            "mutation_status",
            sa.String(length=40),
            server_default="idle",
            nullable=False,
        ),
        sa.Column("mutation_token", sa.Uuid(), nullable=True),
        sa.Column("fence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("owner", sa.String(length=160), nullable=True),
        sa.Column("operation", sa.String(length=80), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manifest_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["bot_versions.id"],
        ),
        sa.PrimaryKeyConstraint("version_id"),
    )
    op.create_table(
        "channel_llm_bindings",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("bot_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("principal_key", sa.String(length=160), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=40), server_default="open", nullable=False
        ),
        sa.Column(
            "provider_config_ref",
            sa.String(length=160),
            server_default="",
            nullable=False,
        ),
        sa.Column("provider_conversation_id", sa.String(length=200), nullable=True),
        sa.Column(
            "provider_state",
            sa.String(length=40),
            server_default="uninitialized",
            nullable=False,
        ),
        sa.Column(
            "snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "committed_turn_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("memory_prefix_hash", sa.String(length=64), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("generation > 0", name="ck_llm_binding_generation"),
        sa.ForeignKeyConstraint(
            ["bot_id"],
            ["bots.id"],
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["channel_rooms.id"],
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["bot_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_config_ref",
            "provider_conversation_id",
            name="uq_llm_provider_conversation",
        ),
        sa.UniqueConstraint(
            "room_id", "principal_key", "generation", name="uq_llm_binding_generation"
        ),
    )
    op.create_index(
        "uq_llm_binding_live",
        "channel_llm_bindings",
        ["room_id", "principal_key"],
        unique=True,
        postgresql_where=sa.text("status <> 'closed'"),
    )
    op.create_table(
        "llm_budget_reservations",
        sa.Column("reservation_group_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("quote_hash", sa.String(length=64), nullable=False),
        sa.Column("amount_upper", sa.BigInteger(), nullable=False),
        sa.Column("actual_amount", sa.BigInteger(), nullable=True),
        sa.Column(
            "state", sa.String(length=40), server_default="reserved", nullable=False
        ),
        sa.Column("receipt_ref", sa.String(length=200), nullable=True),
        sa.Column(
            "usage_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_upper >= 0 AND (actual_amount IS NULL OR actual_amount >= 0)",
            name="ck_llm_reservation_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["llm_budget_accounts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["channel_queue_events.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_id", "account_id", name="uq_llm_budget_event_account"
        ),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column(
            "operation_kind",
            sa.String(length=40),
            server_default="channel",
            nullable=False,
        ),
    )
    op.add_column(
        "channel_queue_events", sa.Column("binding_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "channel_queue_events",
        sa.Column("client_message_key", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column("request_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column(
            "phase", sa.String(length=40), server_default="queued", nullable=False
        ),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column("lease_fence", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column("provider_response_id", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "channel_queue_events",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_queue_persistent_message",
        "channel_queue_events",
        ["binding_id", "client_message_key"],
        unique=True,
        postgresql_where=sa.text(
            "operation_kind = 'persistent_llm' AND client_message_key IS NOT NULL"
        ),
    )
    op.create_foreign_key(
        "fk_queue_llm_binding",
        "channel_queue_events",
        "channel_llm_bindings",
        ["binding_id"],
        ["id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        "channel_llm_bindings",
        "channel_llm_version_guards",
        "llm_budget_accounts",
        "llm_budget_reservations",
    ):
        if bind.execute(
            sa.text("SELECT EXISTS (SELECT 1 FROM " + table + ")")
        ).scalar():
            raise RuntimeError("지속 대화 자료를 보존/정리한 뒤 downgrade해야 합니다.")
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM channel_queue_events WHERE operation_kind <> 'channel')"
        )
    ).scalar():
        raise RuntimeError("지속 대화 event를 먼저 확인해야 합니다.")
    op.drop_constraint(
        "fk_queue_llm_binding", "channel_queue_events", type_="foreignkey"
    )
    op.drop_index(
        "uq_queue_persistent_message",
        table_name="channel_queue_events",
        postgresql_where=sa.text(
            "operation_kind = 'persistent_llm' AND client_message_key IS NOT NULL"
        ),
    )
    op.drop_column("channel_queue_events", "cancel_requested_at")
    op.drop_column("channel_queue_events", "provider_response_id")
    op.drop_column("channel_queue_events", "submitted_at")
    op.drop_column("channel_queue_events", "lease_expires_at")
    op.drop_column("channel_queue_events", "lease_fence")
    op.drop_column("channel_queue_events", "lease_owner")
    op.drop_column("channel_queue_events", "phase")
    op.drop_column("channel_queue_events", "request_hash")
    op.drop_column("channel_queue_events", "client_message_key")
    op.drop_column("channel_queue_events", "binding_id")
    op.drop_column("channel_queue_events", "operation_kind")
    op.drop_table("llm_budget_reservations")
    op.drop_index(
        "uq_llm_binding_live",
        table_name="channel_llm_bindings",
        postgresql_where=sa.text("status <> 'closed'"),
    )
    op.drop_table("channel_llm_bindings")
    op.drop_table("channel_llm_version_guards")
    op.drop_table("llm_budget_accounts")
