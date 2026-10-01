"""Completion guards: declared hashes, grant expiry, exact pinned metadata, no I/O."""

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from packages.domain.security import ServiceError
from packages.domain.uploads import (
    owner_reference,
    require_completion,
    require_staging_match,
    staging_key,
    validate_complete_request,
)
from packages.storage.s3 import ObjectVersion

NOW = datetime(2026, 10, 1, 2, 0)
TENANT, UPLOAD, ITEM = str(uuid4()), str(uuid4()), str(uuid4())
ROW = {
    "id": UPLOAD,
    "tenant_id": TENANT,
    "inspection_item_id": ITEM,
    "remediation_task_id": None,
    "object_key": staging_key(TENANT, UPLOAD),
    "expected_sha256": "a" * 64,
    "status": "granted",
    "expires_at": NOW + timedelta(seconds=1),
    "size_bytes": 100,
    "mime_type": "image/png",
}
PINNED = ObjectVersion(ROW["object_key"], "fixed-version", 100, "image/png")


@pytest.mark.parametrize(
    "body",
    [
        {},
        None,
        {"upload_id": UPLOAD},
        {"upload_id": UPLOAD, "sha256": "A" * 64},
        {"upload_id": "bad", "sha256": "a" * 64},
        {"upload_id": UPLOAD.upper(), "sha256": "a" * 64},
        {"upload_id": UPLOAD, "sha256": "a" * 64, "version_id": "untrusted"},
    ],
)
def test_completion_body_is_exact(body):
    with pytest.raises(ServiceError) as error:
        validate_complete_request(body)
    assert error.value.status == 422


def test_granted_matching_metadata_is_only_acceptance_not_image_validation():
    validate_complete_request({"upload_id": UPLOAD, "sha256": "a" * 64})
    require_completion(ROW, "a" * 64, NOW)
    require_staging_match(ROW, PINNED)
    assert ROW["status"] == "granted"


@pytest.mark.parametrize("status", ["validating", "ready", "rejected", "expired"])
def test_non_granted_without_existing_image_is_not_new_work(status):
    with pytest.raises(ServiceError) as error:
        require_completion({**ROW, "status": status}, "a" * 64, NOW)
    assert error.value.code == "STATE_CONFLICT"


@pytest.mark.parametrize("delta", [0, -1, -600])
def test_expiry_is_exclusive(delta):
    with pytest.raises(ServiceError) as error:
        require_completion({**ROW, "expires_at": NOW + timedelta(seconds=delta)}, "a" * 64, NOW)
    assert error.value.status == 409


@pytest.mark.parametrize("status", ["validating", "ready", "rejected"])
def test_existing_image_does_not_recheck_original_grant_expiry(status):
    row = {**ROW, "status": status, "expires_at": NOW - timedelta(hours=1)}
    require_completion(row, "a" * 64, NOW, already_completed=True)
    with pytest.raises(ServiceError) as error:
        require_completion(row, "b" * 64, NOW, already_completed=True)
    assert error.value.code == "HASH_MISMATCH"


@pytest.mark.parametrize("status", ["granted", "expired"])
def test_existing_image_requires_consistent_persisted_state(status):
    with pytest.raises(RuntimeError):
        require_completion({**ROW, "status": status}, "a" * 64, NOW, already_completed=True)


@pytest.mark.parametrize(
    "values",
    [
        {"version_id": ""},
        {"version_id": "null"},
        {"version_id": "x" * 201},
        {"version_id": "x\n"},
        {"version_id": None},
        {"key": "other"},
        {"size_bytes": 99},
        {"size_bytes": 101},
        {"size_bytes": True},
        {"mime_type": "image/jpeg"},
        {"mime_type": "image/gif"},
    ],
)
def test_head_metadata_must_match_exact_grant(values):
    with pytest.raises(ServiceError) as error:
        require_staging_match(ROW, replace(PINNED, **values))
    assert error.value.code == "IMAGE_INVALID"


def test_invalid_persisted_reference_is_not_a_client_controlled_object():
    with pytest.raises(RuntimeError):
        require_completion({**ROW, "object_key": "other"}, "a" * 64, NOW)
    assert owner_reference(ROW) == ("inspection_item", ITEM)
    assert owner_reference({**ROW, "inspection_item_id": None, "remediation_task_id": ITEM}) == (
        "remediation_task",
        ITEM,
    )
    for row in ({**ROW, "inspection_item_id": None}, {**ROW, "remediation_task_id": ITEM}):
        with pytest.raises(RuntimeError):
            owner_reference(row)
