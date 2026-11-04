"""Protection Engine 2.0; independent node safety and sourced integrity history."""

from alembic import op
import sqlalchemy as sa
import importlib.util
from pathlib import Path

revision = "0004"
down_revision = "0003"
branch_labels = depends_on = None
TABLES = (
    "safety_policy",
    "node_safety",
    "integrity_checks",
    "safety_decisions",
    "protection_engine_state",
)


def metadata():
    spec = importlib.util.spec_from_file_location(
        "schema_safety_v2", Path(__file__).parents[1] / "schema_safety_v2.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Base.metadata


def upgrade():
    schema = metadata()
    for name in TABLES:
        schema.tables[name].create(op.get_bind())
    op.execute(
        sa.text(
            "INSERT INTO safety_policy (id,cpu_temperature_limit,gpu_temperature_limit,ram_percent_limit,min_ram_available_gb,min_disk_free_gb,cpu_percent_limit,sustained_cpu_seconds,require_integrity_for_games,max_integrity_age_seconds,minimum_agent_version) VALUES (1,95,90,97,0.5,0.5,98,60,false,900,'')"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO protection_engine_state (id,nodes_evaluated,accounts_evaluated) VALUES (1,0,0)"
        )
    )


def downgrade():
    schema = metadata()
    for name in reversed(TABLES):
        schema.tables[name].drop(op.get_bind())
