"""Allow independent OCR evidence while preserving historical derivative rows."""

import os

from alembic import op

revision = "0004_ocr_evidence"
down_revision = "0003_run_image_version"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE image_derivatives MODIFY COLUMN detection_id CHAR(36) NULL, "
        "ADD COLUMN line_id CHAR(36) NULL AFTER detection_id, "
        "ADD COLUMN size_bytes BIGINT UNSIGNED NULL AFTER line_id"
    )


def downgrade():
    schema = op.get_bind().engine.url.database
    if (
        os.environ.get("APP_ENV") != "test"
        or os.environ.get("LABSAFE_ALLOW_DESTRUCTIVE_DOWNGRADE") != schema
    ):
        raise RuntimeError("Destructive downgrade requires explicitly confirmed test schema")
    if (
        op.get_bind()
        .exec_driver_sql("SELECT COUNT(*) FROM image_derivatives WHERE detection_id IS NULL")
        .scalar()
    ):
        raise RuntimeError("Independent OCR evidence requires forward repair; downgrade refused")
    op.execute(
        "ALTER TABLE image_derivatives DROP COLUMN line_id, DROP COLUMN size_bytes, "
        "MODIFY COLUMN detection_id CHAR(36) NOT NULL"
    )
