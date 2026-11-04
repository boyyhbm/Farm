"""Use a persistent latest-check pointer instead of wall-clock ordering (Windows clock ties)."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "protection_engine_state",
        sa.Column("last_manager_check_id", sa.String(36), nullable=True),
    )
    # Older observations with tied timestamps are treated conservatively until the next check.
    op.execute(
        sa.text(
            "UPDATE protection_engine_state SET last_manager_check_id=(SELECT id FROM integrity_checks WHERE node_id IS NULL ORDER BY received_at DESC, CASE WHEN status='FAILED' THEN 1 ELSE 0 END DESC, id DESC LIMIT 1)"
        )
    )


def downgrade():
    op.drop_column("protection_engine_state", "last_manager_check_id")
