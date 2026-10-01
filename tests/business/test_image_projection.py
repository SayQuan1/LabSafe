"""Image projection must be JSON-safe before storing the idempotent response."""

from datetime import datetime
from uuid import uuid4

import pytest

from packages.persistence.images import ImageRepository
from packages.shared.json_hash import canonical_json


@pytest.mark.parametrize("owner", ["inspection_item", "remediation_task"])
@pytest.mark.parametrize("status", ["validating", "ready", "rejected", "deleted"])
def test_projection_has_only_contract_fields_and_all_dates_are_utc_strings(owner, status):
    now, owner_id = datetime(2026, 10, 1, 2, 3, 4, 123000), str(uuid4())
    row = {
        "id": str(uuid4()),
        "created_at": now,
        "updated_at": now,
        "version": 1,
        "laboratory_id": str(uuid4()),
        "status": status,
        "original_sha256": "a" * 64,
        "analysis_sha256": None,
        "width": None,
        "height": None,
        "mime_type": "image/png",
        "captured_at": now,
        "inspection_item_id": owner_id if owner == "inspection_item" else None,
        "remediation_task_id": owner_id if owner == "remediation_task" else None,
        "tenant_id": str(uuid4()),
        "upload_id": str(uuid4()),
        "original_key": "internal-key",
        "original_object_version": "internal-version",
        "legal_hold": True,
    }
    projected = ImageRepository.project(row)
    for field in ("created_at", "updated_at", "captured_at"):
        assert projected[field] == "2026-10-01T02:03:04.123Z"
    assert projected["owner_type"] == owner and projected["owner_id"] == owner_id
    assert set(projected) == {
        "id",
        "created_at",
        "updated_at",
        "version",
        "owner_type",
        "owner_id",
        "laboratory_id",
        "status",
        "original_sha256",
        "analysis_sha256",
        "width",
        "height",
        "mime_type",
        "captured_at",
    }
    encoded = canonical_json({"data": projected, "request_id": str(uuid4())})
    assert "internal-key" not in encoded and "internal-version" not in encoded
