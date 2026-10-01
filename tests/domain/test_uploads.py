"""Upload ownership/state, metadata and quota policies without I/O."""

from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from packages.domain.security import ServiceError
from packages.domain.uploads import (
    CAPTURE_STATES,
    EVIDENCE_STATES,
    MAX_BYTES,
    authorize_upload,
    capture_time,
    require_grant_capacity,
    staging_key,
    validate_upload_request,
)
from packages.domain.workflow import ItemStatus, TaskStatus
from tests.domain.test_workflow import ADMIN, INSPECTOR, OTHER_INSPECTOR, REMEDIATOR, item, task


def upload_body(owner_id=None, **overrides):
    return {
        "owner_type": "inspection_item",
        "owner_id": owner_id or str(uuid4()),
        "filename": "测试图片.png",
        "mime_type": "image/png",
        "size_bytes": 100,
        "sha256": "a" * 64,
        "captured_at": "2026-10-01T10:00:00.123+08:00",
        **overrides,
    }


@pytest.mark.parametrize("status", list(ItemStatus))
@pytest.mark.parametrize("parent", ["draft", "in_progress", "completed", "cancelled"])
@pytest.mark.parametrize("actor", [INSPECTOR, OTHER_INSPECTOR, ADMIN, REMEDIATOR])
def test_capture_state_and_creator_matrix(status, parent, actor):
    snapshot = item(status=status, inspection_status=parent)
    if (
        actor in (INSPECTOR, ADMIN)
        and status in CAPTURE_STATES
        and parent in {"draft", "in_progress"}
    ):
        assert authorize_upload(actor, snapshot) is None
    else:
        with pytest.raises(ServiceError) as caught:
            authorize_upload(actor, snapshot)
        assert caught.value.status == (403 if actor in (OTHER_INSPECTOR, REMEDIATOR) else 409)


@pytest.mark.parametrize("status", list(TaskStatus))
@pytest.mark.parametrize(
    "actor", [REMEDIATOR, ADMIN, INSPECTOR, replace(REMEDIATOR, user_id="other")]
)
def test_evidence_task_state_and_assignee_matrix(status, actor):
    if actor == REMEDIATOR and status in EVIDENCE_STATES:
        assert authorize_upload(actor, task(status=status)) is None
    else:
        with pytest.raises(ServiceError) as caught:
            authorize_upload(actor, task(status=status))
        assert caught.value.status == (409 if actor == REMEDIATOR else 403)


@pytest.mark.parametrize(
    "owner",
    [
        item(tenant_id="foreign"),
        item(laboratory_id="foreign"),
        task(tenant_id="foreign"),
        task(laboratory_id="foreign"),
    ],
)
def test_cross_scope_owner_is_hidden(owner):
    with pytest.raises(ServiceError) as caught:
        authorize_upload(REMEDIATOR, owner)
    assert caught.value.status == 404


def test_replay_rechecks_authority_without_rerunning_state_or_creating_new_intent():
    assert authorize_upload(INSPECTOR, item(status="completed"), check_state=False) is None
    with pytest.raises(ServiceError) as caught:
        authorize_upload(OTHER_INSPECTOR, item(status="completed"), check_state=False)
    assert caught.value.status == 403


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("owner_type", "file", "VALIDATION_ERROR"),
        ("owner_id", "../x", "VALIDATION_ERROR"),
        ("filename", "", "VALIDATION_ERROR"),
        ("filename", "a" * 256, "VALIDATION_ERROR"),
        ("filename", "../x.png", "VALIDATION_ERROR"),
        ("filename", "C:\\x.png", "VALIDATION_ERROR"),
        ("filename", "x\n.png", "VALIDATION_ERROR"),
        ("filename", "..", "VALIDATION_ERROR"),
        ("size_bytes", True, "VALIDATION_ERROR"),
        ("size_bytes", "1", "VALIDATION_ERROR"),
        ("size_bytes", 0, "VALIDATION_ERROR"),
        ("size_bytes", MAX_BYTES + 1, "IMAGE_TOO_LARGE"),
        ("mime_type", "image/svg+xml", "UNSUPPORTED_MEDIA_TYPE"),
        ("sha256", "A" * 64, "VALIDATION_ERROR"),
        ("sha256", "a" * 64 + "\n", "VALIDATION_ERROR"),
        ("captured_at", "2026-10-01", "VALIDATION_ERROR"),
        ("captured_at", "2026-10-01T10:00:00", "VALIDATION_ERROR"),
        ("captured_at", "0999-01-01T00:00:00Z", "VALIDATION_ERROR"),
        ("captured_at", "2026-02-30T00:00:00Z", "VALIDATION_ERROR"),
        ("captured_at", "2026-10-01T00:00:00+00:60", "VALIDATION_ERROR"),
        ("captured_at", "2026-10-01T00:00:00+24:00", "VALIDATION_ERROR"),
        ("owner_type", [], "VALIDATION_ERROR"),
        ("mime_type", [], "UNSUPPORTED_MEDIA_TYPE"),
    ],
)
def test_invalid_upload_metadata(field, value, code):
    with pytest.raises(ServiceError) as caught:
        validate_upload_request(upload_body(**{field: value}))
    assert caught.value.code == code


def test_valid_sizes_media_and_utc_milliseconds_are_preserved():
    for size in (1, MAX_BYTES):
        for mime in ("image/jpeg", "image/png", "image/webp"):
            assert validate_upload_request(
                upload_body(size_bytes=size, mime_type=mime)
            ) == datetime(2026, 10, 1, 2, 0, 0, 123000)
    assert capture_time("2026-10-01t02:00:00.123999z") == datetime(2026, 10, 1, 2, 0, 0, 123000)
    for body in ([], {**upload_body(), "tenant_id": str(uuid4())}, {}):
        with pytest.raises(ServiceError):
            validate_upload_request(body)


def test_quota_expiration_boundary_and_key_path_are_server_owned():
    now = datetime(2026, 10, 1)
    require_grant_capacity([now] * 10, now)
    require_grant_capacity([now + timedelta(seconds=20)] * 9, now)
    with pytest.raises(ServiceError) as caught:
        require_grant_capacity(
            [now + timedelta(seconds=20)] * 9 + [now + timedelta(milliseconds=1500)], now
        )
    assert caught.value.status == 429 and caught.value.retry_after == 2
    tenant, upload = str(uuid4()), str(uuid4())
    assert staging_key(tenant, upload) == f"staging/{tenant}/{upload}"
    for value in ("../x", tenant.upper(), tenant.replace("-", "")):
        with pytest.raises(ServiceError):
            staging_key(value, upload)
