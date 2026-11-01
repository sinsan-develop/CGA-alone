"""연결의 설치 lease·전달 사실·기존 방 매핑만 추가한다."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision="20261102_0031"
down_revision="20261102_0030"
branch_labels=None
depends_on=None


def identity_columns():
    return [sa.Column("id",sa.Uuid(),primary_key=True),sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),sa.Column("updated_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now())]


def upgrade():
    op.add_column("channel_connections",sa.Column("accept_after",sa.DateTime(timezone=True)))
    op.create_table("channel_installation_leases",*identity_columns(),
        sa.Column("organization_id",sa.Uuid(),sa.ForeignKey("organizations.id"),nullable=False),
        sa.Column("provider",sa.String(40),nullable=False),sa.Column("installation_id",sa.String(200),nullable=False),sa.Column("account_id",sa.String(200),nullable=False),
        sa.Column("owner",sa.String(120)),sa.Column("fence",sa.BigInteger(),nullable=False,server_default="0"),sa.Column("lease_expires_at",sa.DateTime(timezone=True)),
        sa.Column("cursor_json",postgresql.JSONB(),nullable=False,server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("organization_id","provider","installation_id","account_id",name="uq_connection_installation"))
    op.create_table("channel_connection_rooms",*identity_columns(),
        sa.Column("connection_id",sa.Uuid(),sa.ForeignKey("channel_connections.id"),nullable=False),sa.Column("native_room_hash",sa.String(64),nullable=False),
        sa.Column("room_id",sa.Uuid(),sa.ForeignKey("channel_rooms.id"),nullable=False),sa.Column("participant_ciphertext",sa.Text()),sa.Column("credential_key_version",sa.String(80)),
        sa.UniqueConstraint("connection_id","native_room_hash",name="uq_connection_native_room"),sa.UniqueConstraint("room_id",name="uq_connected_existing_room"))
    op.create_table("channel_connection_deliveries",*identity_columns(),
        sa.Column("organization_id",sa.Uuid(),sa.ForeignKey("organizations.id"),nullable=False),sa.Column("connection_id",sa.Uuid(),sa.ForeignKey("channel_connections.id"),nullable=False),
        sa.Column("generation",sa.BigInteger(),nullable=False),sa.Column("installation_fence",sa.BigInteger(),nullable=False),sa.Column("native_room_hash",sa.String(64),nullable=False),sa.Column("native_actor_hash",sa.String(64),nullable=False),sa.Column("event_key",sa.String(64),nullable=False),sa.Column("request_hash",sa.String(64),nullable=False),
        sa.Column("status",sa.String(20),nullable=False,server_default="received"),sa.Column("event_json",postgresql.JSONB(),nullable=False),sa.Column("result_json",postgresql.JSONB(),nullable=False,server_default=sa.text("'{}'::jsonb")),
        sa.Column("context_ciphertext",sa.Text()),sa.Column("context_key_version",sa.String(80)),sa.Column("execution_claim_id",sa.String(36)),sa.Column("send_claim_id",sa.String(36)),sa.Column("deadline_at",sa.DateTime(timezone=True)),
        sa.Column("provider_message_ids",postgresql.JSONB(),nullable=False,server_default=sa.text("'[]'::jsonb")),sa.Column("last_error_code",sa.String(80)),
        sa.Column("room_id",sa.Uuid(),sa.ForeignKey("channel_rooms.id")),sa.Column("request_message_id",sa.Uuid(),sa.ForeignKey("channel_messages.id")),sa.Column("queue_event_id",sa.Uuid(),sa.ForeignKey("channel_queue_events.id")),
        sa.UniqueConstraint("connection_id","event_key",name="uq_connection_delivery_event"),sa.CheckConstraint("status IN ('received','executing','ready','sending','sent','failed','unknown','cancelled')",name="ck_connection_delivery_status"))
    op.create_index("ix_connection_delivery_pending","channel_connection_deliveries",["connection_id","status","created_at"])


def downgrade():
    db=op.get_bind()
    for table in ["channel_connection_deliveries","channel_connection_rooms","channel_installation_leases"]:
        if db.execute(sa.text("SELECT EXISTS(SELECT 1 FROM "+table+")")).scalar():raise RuntimeError("Connection runtime records exist; preservation approval required")
    op.drop_table("channel_connection_deliveries");op.drop_table("channel_connection_rooms");op.drop_table("channel_installation_leases")
    op.drop_column("channel_connections","accept_after")
