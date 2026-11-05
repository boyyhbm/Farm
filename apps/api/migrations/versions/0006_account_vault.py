"""Non-sensitive account vault metadata; encrypted payloads stay in local SQLite."""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "vault_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("login", sa.String(100), nullable=False),
        sa.Column("login_key", sa.String(100), nullable=False),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("steam_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("has_password", sa.Boolean(), nullable=False),
        sa.Column("has_guard", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.String(36), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_attempt", sa.DateTime(timezone=True)),
        sa.Column("last_auth_error", sa.String(50)),
        sa.UniqueConstraint("owner_id", "login_key"),
        sa.UniqueConstraint("owner_id", "account_id"),
    )
    for name in ("owner_id", "account_id", "archived", "created_at"):
        op.create_index("ix_vault_records_" + name, "vault_records", [name])
    op.create_table(
        "vault_imports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("auth_session_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("imported", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.Column("group_id", sa.String(36), sa.ForeignKey("account_groups.id")),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("nodes.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for name in ("owner_id", "status", "created_at"):
        op.create_index("ix_vault_imports_" + name, "vault_imports", [name])


def downgrade():
    op.drop_table("vault_imports")
    op.drop_table("vault_records")
