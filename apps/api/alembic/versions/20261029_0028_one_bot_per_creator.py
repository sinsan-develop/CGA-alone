"""제작자별 미삭제 봇 하나: 기존 CGA 데이터 이관/정정 없음."""
from alembic import op
import sqlalchemy as sa

revision = "20261029_0028"
down_revision = "20260720_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_bots_live_creator", "bots", ["created_by"], unique=True,
                    postgresql_where=sa.text("created_by IS NOT NULL AND deleted_at IS NULL"))


def downgrade() -> None:
    op.drop_index("uq_bots_live_creator", table_name="bots")
