"""Actual image handler + real MySQL; object transport is synthetic unless explicitly stated."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import event, text

from packages.application.image_validation import prepare_image
from packages.application.job_execution import ImageExecution
from packages.domain.image_validation import (
    CONTENT_ERRORS,
    RejectedImage,
    ValidatedImage,
    image_keys,
)
from packages.domain.job_execution import LeaseLost
from packages.domain.security import ServiceError
from packages.persistence.image_validation import write_image_result
from tests.business.test_image_validation import encoded
from tests.persistence.factories import insert
from tests.persistence.test_job_execution_mysql import case, claim, expire, row, update

# Importing case intentionally shares the same pre-validation graph across F3 tests.
__all__ = ["case"]


def outcome(lease):
    sha = "b" * 64
    original, analysis = image_keys(lease.tenant_id, lease.input, sha)
    return ValidatedImage(original, "original-v1", analysis, "analysis-v1", sha, 7, 5)


def writer(lease, result):
    return lambda connection, input: write_image_result(
        connection, lease.tenant_id, lease.task_id, input, result
    )


def events(database, case):
    with database.connect() as connection:
        return [
            json.loads(v)
            for v in connection.scalars(
                text(
                    "SELECT payload FROM outbox_events WHERE tenant_id=:tenant "
                    "AND event_type='ImageValidated'"
                ),
                {"tenant": case.tenant},
            )
        ]


def audits(database, case):
    with database.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT actor_id,changes,request_id FROM audit_events WHERE tenant_id=:tenant "
                    "AND action='image.validate'"
                ),
                {"tenant": case.tenant},
            ).mappings()
        )


def test_ready_transaction_registers_evidence_and_valid_event(database, case):
    lease = claim(database, case)
    result = outcome(lease)
    ImageExecution(database).commit(lease, writer(lease, result))
    image = row(database, "asset_images", case.image)
    assert image["status"] == row(database, "uploads", case.upload)["status"] == "ready"
    assert image["original_key"] == result.original_key
    assert image["original_object_version"] == "original-v1"
    assert image["analysis_key"] == result.analysis_key
    assert image["analysis_object_version"] == "analysis-v1"
    assert image["analysis_sha256"] == result.analysis_sha256
    assert (image["width"], image["height"], image["version"]) == (
        7,
        5,
        lease.input.image_version + 1,
    )
    assert row(database, "inspection_items", case.item)["status"] == "uploaded"
    assert row(database, "inspections", case.inspection)["status"] == "in_progress"
    assert row(database, "task_runs", case.job)["state"] == "succeeded"
    notices = events(database, case)
    assert len(notices) == 1
    schema = json.loads(Path("contracts/events-v1.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(notices[0])
    assert notices[0]["aggregate_version"] == image["version"]
    assert notices[0]["trace_id"] == case.message["trace_id"]
    audit = audits(database, case)
    assert len(audit) == 1 and audit[0]["actor_id"] is None
    assert json.loads(audit[0]["changes"])["status"] == "ready"
    with pytest.raises(LeaseLost):
        ImageExecution(database).commit(lease, writer(lease, result))
    assert len(events(database, case)) == len(audits(database, case)) == 1


@pytest.mark.parametrize("status", ["uploaded", "needs_retake", "needs_review", "failed"])
def test_success_preserves_other_capture_states(database, case, status):
    update(
        database,
        "UPDATE inspection_items SET status=:status WHERE id=:id",
        id=case.item,
        status=status,
    )
    lease = claim(database, case)
    ImageExecution(database).commit(lease, writer(lease, outcome(lease)))
    assert row(database, "inspection_items", case.item)["status"] == status


@pytest.mark.parametrize("code", sorted(CONTENT_ERRORS))
@pytest.mark.parametrize("other_ready", [False, True])
def test_content_rejection_is_atomic_without_fake_success_event(database, case, code, other_ready):
    if other_ready:
        with database.begin() as connection:
            upload = insert(
                connection,
                "uploads",
                tenant_id=case.tenant,
                laboratory_id=case.lab,
                inspection_item_id=case.item,
                requested_by=case.user,
                status="ready",
            )
            insert(
                connection,
                "asset_images",
                tenant_id=case.tenant,
                laboratory_id=case.lab,
                inspection_item_id=case.item,
                upload_id=upload,
                status="ready",
            )
    lease = claim(database, case)
    ImageExecution(database).commit(lease, writer(lease, RejectedImage(code)))
    image = row(database, "asset_images", case.image)
    assert image["status"] == row(database, "uploads", case.upload)["status"] == "rejected"
    assert image["analysis_key"] is image["analysis_sha256"] is image["width"] is None
    assert row(database, "inspection_items", case.item)["status"] == (
        "uploaded" if other_ready else "draft"
    )
    assert row(database, "task_runs", case.job)["state"] == "succeeded"
    assert events(database, case) == []
    assert json.loads(audits(database, case)[0]["changes"])["error_code"] == code


@pytest.mark.parametrize("status", ["in_progress", "rejected"])
@pytest.mark.parametrize("ready", [False, True])
def test_remediation_image_keeps_owner_state(database, case, status, ready):
    with database.begin() as connection:
        connection.execute(
            text("UPDATE remediation_tasks SET status=:status WHERE id=:id"),
            {"id": case.task, "status": status},
        )
        for table in ("uploads", "asset_images"):
            connection.execute(
                text(
                    f"UPDATE {table} SET inspection_item_id=NULL,"
                    "remediation_task_id=:task WHERE id=:id"
                ),
                {"task": case.task, "id": case.upload if table == "uploads" else case.image},
            )
    lease = claim(database, case)
    assert lease.input.owner_type == "remediation_task"
    ImageExecution(database).commit(
        lease, writer(lease, outcome(lease) if ready else RejectedImage("IMAGE_INVALID"))
    )
    assert row(database, "remediation_tasks", case.task)["status"] == status
    assert len(events(database, case)) == int(ready)


@pytest.mark.parametrize(
    "prefix",
    [
        "UPDATE uploads",
        "UPDATE inspection_items",
        "UPDATE inspections",
        "INSERT INTO audit_events",
        "INSERT INTO outbox_events",
        "UPDATE task_attempts",
    ],
)
def test_result_failure_rolls_back_entire_ready_transaction(database, case, prefix):
    lease = claim(database, case)
    before_image = row(database, "asset_images", case.image)
    before_owner = row(database, "inspection_items", case.item)
    before_upload = row(database, "uploads", case.upload)

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(prefix):
            raise RuntimeError("injected")

    event.listen(database, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            ImageExecution(database).commit(lease, writer(lease, outcome(lease)))
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert row(database, "asset_images", case.image) == before_image
    assert row(database, "uploads", case.upload) == before_upload
    assert row(database, "inspection_items", case.item) == before_owner
    assert row(database, "inspections", case.inspection)["status"] == "draft"
    assert row(database, "task_runs", case.job)["state"] == "leased"
    assert events(database, case) == audits(database, case) == []


@pytest.mark.parametrize("kind", ["expired", "cancelled", "superseded_input"])
def test_prepared_objects_cannot_register_after_fence_invalidated(database, case, kind):
    lease = claim(database, case)
    result = outcome(lease)
    if kind == "expired":
        expire(database, case)
    elif kind == "cancelled":
        update(
            database, "UPDATE inspections SET status='cancelled' WHERE id=:id", id=case.inspection
        )
    else:
        update(database, "UPDATE asset_images SET version=version+1 WHERE id=:id", id=case.image)
    with pytest.raises(LeaseLost):
        ImageExecution(database).commit(lease, writer(lease, result))
    assert row(database, "asset_images", case.image)["status"] == "validating"
    assert events(database, case) == audits(database, case) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("analysis_key", "other/key"),
        ("original_version", "null"),
        ("analysis_sha256", "invalid"),
        ("width", 10001),
        ("height", True),
    ],
)
def test_malformed_server_result_never_commits(database, case, field, value):
    lease = claim(database, case)
    with pytest.raises((ValueError, ServiceError)):
        ImageExecution(database).commit(
            lease, writer(lease, replace(outcome(lease), **{field: value}))
        )
    assert row(database, "asset_images", case.image)["status"] == "validating"
    assert events(database, case) == audits(database, case) == []


def test_two_duplicate_consumers_commit_one_image_result(database, case):
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return ImageExecution(database).execute(
            case.message,
            lambda input, cancelled: RejectedImage("IMAGE_INVALID"),
            lambda connection, input, result: write_image_result(
                connection, case.tenant, case.job, input, result
            ),
        )

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(lambda _: run(), range(2))) == 1
    assert len(audits(database, case)) == 1


def test_real_decode_to_ready_with_external_storage_outside_transaction(database, case):
    payload = encoded()
    sha = hashlib.sha256(payload).hexdigest()
    update(
        database,
        "UPDATE uploads SET expected_sha256=:sha,size_bytes=:size WHERE id=:id",
        id=case.upload,
        sha=sha,
        size=len(payload),
    )
    update(
        database,
        "UPDATE asset_images SET original_sha256=:sha WHERE id=:id",
        id=case.image,
        sha=sha,
    )
    storage = Mock()

    def download(*args):
        with database.begin() as connection:
            connection.execute(
                text("SELECT id FROM asset_images WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": case.image},
            )
            connection.execute(
                text("SELECT id FROM task_runs WHERE id=:id FOR UPDATE NOWAIT"), {"id": case.job}
            )
        return payload

    storage.read_staging.side_effect = download
    storage.preserve_original.return_value = "original-real-decode-v1"
    storage.put_analysis.return_value = "analysis-real-decode-v1"
    assert ImageExecution(database).execute(
        case.message,
        lambda input, cancelled: prepare_image(storage, case.tenant, input),
        lambda connection, input, result: write_image_result(
            connection, case.tenant, case.job, input, result
        ),
    )
    assert row(database, "asset_images", case.image)["status"] == "ready"
    assert len(events(database, case)) == 1


def test_registered_celery_task_consumes_real_redis_message(
    database, case, isolated_redis, monkeypatch
):
    from celery.contrib.testing.worker import start_worker

    from apps.worker.app.main import create_celery_app
    from apps.worker.dispatch import CeleryDispatchTransport
    from packages.application.dispatch import DispatchPublisher
    from packages.storage.s3 import S3Settings
    from tests.business.test_s3_storage import settings

    port = isolated_redis.connection_pool.connection_kwargs["port"]
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
    # Publisher child stays dispatch-only; consumer explicitly opts in below.
    monkeypatch.setenv("WORKER_IMAGE_VALIDATION_ENABLED", "0")
    assert DispatchPublisher(database, CeleryDispatchTransport()).publish_batch(1) == 1
    monkeypatch.setenv("WORKER_IMAGE_VALIDATION_ENABLED", "1")
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://labsafe.test")
    with patch.object(S3Settings, "from_environment", return_value=settings()):
        app = create_celery_app()
        try:
            with (
                patch("packages.persistence.database.database_engine", return_value=database),
                patch(
                    "apps.worker.image_validation.BoundedImagePrepare",
                    return_value=lambda input, cancelled: RejectedImage("IMAGE_INVALID"),
                ),
                start_worker(
                    app,
                    pool="solo",
                    concurrency=1,
                    queues=["q.general"],
                    perform_ping_check=False,
                    loglevel="WARNING",
                    shutdown_timeout=10,
                ),
            ):
                deadline = time.monotonic() + 15
                while row(database, "task_runs", case.job)["state"] != "succeeded":
                    assert time.monotonic() < deadline, "Worker failed to consume dispatch"
                    time.sleep(0.05)
                assert row(database, "asset_images", case.image)["status"] == "rejected"
                assert len(audits(database, case)) == 1
        finally:
            app.close()


def test_bare_ready_status_is_not_a_valid_committed_image(database, case):
    lease = claim(database, case)

    def incomplete(connection, input):
        for table, key in (("asset_images", input.image_id), ("uploads", input.upload_id)):
            connection.execute(text(f"UPDATE {table} SET status='ready' WHERE id=:id"), {"id": key})

    with pytest.raises((ValueError, ServiceError)):
        ImageExecution(database).commit(lease, incomplete)
    assert row(database, "asset_images", case.image)["status"] == "validating"
    assert row(database, "uploads", case.upload)["status"] == "validating"
    assert row(database, "task_runs", case.job)["state"] == "leased"
