"""Initial relational schema; immutable v1 metadata snapshot is stored next to this migration."""

from alembic import op
from sqlalchemy import MetaData
import importlib.util
from pathlib import Path

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def metadata():
    spec = importlib.util.spec_from_file_location(
        "schema_v1", Path(__file__).parents[1] / "schema_v1.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Base.metadata


def upgrade():
    metadata().create_all(op.get_bind())


def downgrade():
    metadata().drop_all(op.get_bind())
