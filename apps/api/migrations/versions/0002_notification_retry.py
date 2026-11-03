"""Bounded Telegram outbox retries."""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "notifications",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "notifications",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_notifications_next_attempt_at", "notifications", ["next_attempt_at"]
    )


def downgrade():
    op.drop_index("ix_notifications_next_attempt_at", table_name="notifications")
    op.drop_column("notifications", "next_attempt_at")
    op.drop_column("notifications", "attempts")
