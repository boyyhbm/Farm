"""Account evidence, incidents and policy; preserve existing data."""

from alembic import op
import sqlalchemy as sa
import importlib.util
from pathlib import Path

revision = "0003"
down_revision = "0002"
branch_labels = depends_on = None


def metadata():
    spec = importlib.util.spec_from_file_location(
        "schema_protection_v1", Path(__file__).parents[1] / "schema_protection_v1.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Base.metadata


def upgrade():
    schema = metadata()
    for name in ("account_risks", "risk_checks", "risk_incidents", "protection_policy"):
        schema.tables[name].create(op.get_bind())
    op.execute(
        sa.text(
            "INSERT INTO protection_policy (id,require_fresh_checks,check_interval_seconds,max_check_age_seconds,provider_status) VALUES (1,false,21600,86400,'NOT_CONFIGURED')"
        )
    )
    op.add_column(
        "nodes",
        sa.Column(
            "transport_security",
            sa.String(40),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "nodes",
        sa.Column("transport_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    condition = sa.text(
        "account_id IS NOT NULL AND status IN ('STARTING','RUNNING','STOPPING','UNKNOWN')"
    )
    op.create_index(
        "one_active_session_per_account",
        "sessions",
        ["account_id"],
        unique=True,
        postgresql_where=condition,
        sqlite_where=condition,
    )


def downgrade():
    op.drop_index("one_active_session_per_account", table_name="sessions")
    op.drop_column("nodes", "transport_checked_at")
    op.drop_column("nodes", "transport_security")
    schema = metadata()
    for name in ("risk_incidents", "risk_checks", "account_risks", "protection_policy"):
        schema.tables[name].drop(op.get_bind())
