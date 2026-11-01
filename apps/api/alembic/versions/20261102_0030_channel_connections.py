"""채널 종류 아래 독립 연결 설정 추가. 기존 설정·방·이력은 변경하지 않는다."""
from alembic import op
import sqlalchemy as sa

revision = "20261102_0030"
down_revision = "20261029_0029"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("channel_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("admin_channels.id"), nullable=False),
        sa.Column("bot_id", sa.Uuid(), sa.ForeignKey("bots.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("installation_id", sa.String(200), nullable=False),
        sa.Column("account_id", sa.String(200), nullable=False),
        sa.Column("target_id", sa.String(200), nullable=False),
        sa.Column("desired_state", sa.String(20), nullable=False, server_default="disconnected"),
        sa.Column("observed_state", sa.String(20), nullable=False, server_default="disconnected"),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("lease_owner", sa.String(120)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("credential_ciphertext", sa.Text()),
        sa.Column("credential_key_version", sa.String(80)),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("updated_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("generation > 0", name="ck_connection_generation"),
        sa.CheckConstraint("desired_state IN ('connected','disconnected')", name="ck_connection_desired"),
        sa.CheckConstraint("observed_state IN ('disconnected','connecting','connected','disconnecting','error')", name="ck_connection_observed"),
    )
    op.create_index("uq_channel_connection_target", "channel_connections", ["organization_id","provider","installation_id","account_id","target_id"], unique=True, postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_index("ix_channel_connections_parent", "channel_connections", ["channel_id","deleted_at"])


def downgrade():
    bind=op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS(SELECT 1 FROM channel_connections)")).scalar():
        raise RuntimeError("Connection records exist; export/preservation approval required before downgrade")
    op.drop_table("channel_connections")
