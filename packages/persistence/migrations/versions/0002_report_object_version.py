"""Pin generated reports to an exact object version; preserve the released baseline."""

import os

from alembic import op

revision = "0002_report_object_version"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE report_exports ADD COLUMN object_version VARCHAR(200) NULL, "
        "ADD COLUMN size_bytes BIGINT UNSIGNED NULL"
    )


def downgrade():
    schema = op.get_bind().engine.url.database
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE") != schema
    ):
        raise RuntimeError("Destructive downgrade requires explicitly confirmed test schema")
    op.execute("ALTER TABLE report_exports DROP COLUMN object_version, DROP COLUMN size_bytes")
