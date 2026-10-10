"""Freeze analysis object versions for new runs; do not guess historical versions."""

import os

from alembic import op

revision = "0003_run_image_version"
down_revision = "0002_report_object_version"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE run_images ADD COLUMN analysis_object_version VARCHAR(200) NULL")


def downgrade():
    schema = op.get_bind().engine.url.database
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE") != schema
    ):
        raise RuntimeError("Destructive downgrade requires explicitly confirmed test schema")
    op.execute("ALTER TABLE run_images DROP COLUMN analysis_object_version")
