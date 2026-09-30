"""I-01B: frozen 43-table design baseline; no demo accounts or credentials."""

import json
import os
from pathlib import Path

from alembic import op
from sqlalchemy import inspect

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None
SNAPSHOTS = Path(__file__).resolve().parents[1] / "snapshots"


def upgrade():
    connection = op.get_bind()
    inspector = inspect(connection)
    if set(inspector.get_table_names()) - {"alembic_version"} or inspector.get_view_names():
        raise RuntimeError("Initial migration requires an empty disposable database")
    ddl = (SNAPSHOTS / "0001_initial.sql").read_text(encoding="utf-8")
    # Immutable generated DDL has no procedures or semicolons in literals.
    ddl = "\n".join(line for line in ddl.splitlines() if not line.startswith("--"))
    for statement in ddl.split(";"):
        if statement.strip():
            connection.exec_driver_sql(statement.strip())


def downgrade():
    connection = op.get_bind()
    schema = connection.engine.url.database
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE") != schema
    ):
        raise RuntimeError(
            "Destructive downgrade requires APP_ENV=test and "
            "LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE equal to DATABASE_SCHEMA"
        )
    model = json.loads((SNAPSHOTS / "0001_initial.json").read_text(encoding="utf-8"))
    for table, definition in model["tables"].items():
        for index, _foreign_key in enumerate(definition["foreign_keys"]):
            op.drop_constraint(f"fk_{table}_{index}", table, type_="foreignkey")
    for table in reversed(model["tables"]):
        op.drop_table(table)
