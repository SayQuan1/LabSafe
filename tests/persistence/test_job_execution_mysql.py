"""Real MySQL, synthetic persisted image inputs; no actual image validator."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from packages.application.dispatch import transaction
from packages.application.job_execution import ImageExecution
from packages.domain.job_execution import InvalidDispatch, LeaseLost
from packages.domain.security import ServiceError
from packages.domain.uploads import staging_key
from packages.persistence import job_execution as jobs
from packages.persistence.image_jobs import enqueue_image_validation
from tests.persistence.factories import Graph


@pytest.fixture
def case(database):
    with database.begin() as connection:
        graph = Graph(connection)
        # This fixture represents pre-validation capture, before any run selects
        # the image. Remove only Graph's synthetic run/image link.
        connection.execute(
            text("DELETE FROM run_images WHERE tenant_id=:tenant AND image_id=:id"),
            {"tenant": graph.tenant, "id": graph.image},
        )
        connection.execute(
            text(
                "UPDATE uploads SET status='validating',object_key=:key,"
                "expected_sha256=:sha,size_bytes=10,mime_type='image/png' "
                "WHERE id=:id"
            ),
            {"id": graph.upload, "sha": "a" * 64, "key": staging_key(graph.tenant, graph.upload)},
        )
        connection.execute(
            text(
                "UPDATE asset_images SET original_key=:key,"
                "original_object_version='pinned-v1',original_sha256=:sha,"
                "mime_type='image/png' WHERE id=:id"
            ),
            {"id": graph.image, "sha": "a" * 64, "key": staging_key(graph.tenant, graph.upload)},
        )
        task = enqueue_image_validation(
            connection,
            tenant_id=graph.tenant,
            upload_id=graph.upload,
            image_id=graph.image,
            request_id=str(uuid4()),
        )
        msg = json.loads(
            connection.scalar(
                text("SELECT payload FROM outbox_events WHERE aggregate_id=:id"), {"id": task}
            )
        )
    graph.job, graph.message = task, msg
    yield graph
    # Retain the graph in this disposable schema; exclude its task from later
    # sweeper tests and never touch unrelated rows from another fixture.
    with database.begin() as connection:
        connection.execute(
            text("DELETE FROM task_attempts WHERE tenant_id=:tenant"), {"tenant": graph.tenant}
        )
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id=:tenant"), {"tenant": graph.tenant}
        )
        connection.execute(
            text("DELETE FROM task_runs WHERE tenant_id=:tenant"), {"tenant": graph.tenant}
        )


def claim(database, case, message=None):
    with transaction(database) as connection:
        return jobs.claim(connection, message or case.message, str(uuid4()))


def row(database, table, key):
    with database.connect() as connection:
        return dict(
            connection.execute(text(f"SELECT * FROM {table} WHERE id=:id"), {"id": key})
            .mappings()
            .one()
        )


def update(database, statement, **params):
    with database.begin() as connection:
        connection.execute(text(statement), params)


def expire(database, case):
    update(
        database,
        "UPDATE task_runs SET lease_until=UTC_TIMESTAMP(3)-INTERVAL 1 SECOND WHERE id=:id",
        id=case.job,
    )


def candidate(database, case):
    with transaction(database) as connection:
        return next(r for r in jobs.expired_candidates(connection) if r["id"] == case.job)


def due(database, case):
    update(
        database,
        "UPDATE task_runs SET available_at=UTC_TIMESTAMP(3)-INTERVAL 1 SECOND WHERE id=:id",
        id=case.job,
    )


def reject_result(connection, input):
    # Synthetic server-owned handler; not the future image validation implementation.
    connection.execute(
        text("UPDATE asset_images SET status='rejected' WHERE id=:id"), {"id": input.image_id}
    )
    connection.execute(
        text("UPDATE uploads SET status='rejected' WHERE id=:id"), {"id": input.upload_id}
    )


def test_claim_is_atomic_with_attempt_and_database_input(database, case):
    lease = claim(database, case)
    assert lease.input.object_version == "pinned-v1" and lease.input.expected_sha256 == "a" * 64
    assert lease.input.key == staging_key(case.tenant, case.upload)
    task = row(database, "task_runs", case.job)
    assert task["attempt"] == task["fencing_token"] == 1
    assert task["lease_until"] - task["heartbeat_at"] == timedelta(seconds=60)
    assert task["state"] == "leased" and task["lease_owner"] == lease.owner
    with database.connect() as connection:
        attempt = (
            connection.execute(
                text("SELECT * FROM task_attempts WHERE task_id=:id"), {"id": case.job}
            )
            .mappings()
            .one()
        )
    assert attempt["status"] == "running" and attempt["fencing_token"] == lease.token
    assert row(database, "asset_images", case.image)["status"] == "validating"


def test_duplicate_consumers_create_one_attempt(database, case):
    barrier = Barrier(2)

    def consume():
        barrier.wait(timeout=5)
        return claim(database, case)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert row(database, "task_runs", case.job)["attempt"] == 1


@pytest.mark.parametrize(
    "field,value,invalid",
    [
        ("replay_generation", 1, True),
        ("dispatch_sequence", 2, True),
        ("resource_id", str(uuid4()), True),
        ("tenant_id", str(uuid4()), False),
    ],
)
def test_message_never_overwrites_persisted_identity(database, case, field, value, invalid):
    message = {**case.message, field: value}
    if field == "resource_id":
        message["payload"] = {**message["payload"], "image_id": value}
    if invalid:
        with pytest.raises(InvalidDispatch):
            claim(database, case, message)
    else:
        assert claim(database, case, message) is None
    assert row(database, "task_runs", case.job)["attempt"] == 0


def test_stale_generation_and_sequence_are_noop(database, case):
    update(database, "UPDATE task_runs SET dispatch_sequence=2 WHERE id=:id", id=case.job)
    assert claim(database, case) is None
    update(database, "UPDATE task_runs SET replay_generation=1 WHERE id=:id", id=case.job)
    assert claim(database, case, {**case.message, "dispatch_sequence": 100}) is None
    assert row(database, "task_runs", case.job)["attempt"] == 0


@pytest.mark.parametrize("state", ["succeeded", "failed", "dead_letter"])
def test_terminal_task_ack_has_no_side_effect(database, case, state):
    update(database, "UPDATE task_runs SET state=:state WHERE id=:id", id=case.job, state=state)
    before = row(database, "task_runs", case.job)
    assert claim(database, case) is None
    assert row(database, "task_runs", case.job) == before


def test_future_retry_not_claimed(database, case):
    update(
        database,
        "UPDATE task_runs SET state='retry_wait',available_at=UTC_TIMESTAMP(3)"
        "+INTERVAL 1 DAY WHERE id=:id",
        id=case.job,
    )
    assert claim(database, case) is None


def test_attempt_insert_failure_rolls_back_claim(database, case):
    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO task_attempts"):
            raise RuntimeError("injected attempt failure")

    event.listen(database, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            claim(database, case)
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert row(database, "task_runs", case.job)["state"] == "ready"
    assert row(database, "task_runs", case.job)["attempt"] == 0


@pytest.mark.parametrize("tamper", ["owner", "token", "generation", "expired"])
def test_old_lease_cannot_renew_fail_or_commit(database, case, tamper):
    lease = claim(database, case)
    if tamper == "expired":
        expire(database, case)
    else:
        lease = replace(lease, **{tamper: str(uuid4()) if tamper == "owner" else 99})
    before = row(database, "task_runs", case.job)
    with transaction(database) as connection:
        assert not jobs.heartbeat(connection, lease)
        assert not jobs.fail(connection, lease, "DEPENDENCY_UNAVAILABLE")
    with pytest.raises(LeaseLost):
        with transaction(database) as connection:
            jobs.commit_result(connection, lease, reject_result)
    assert row(database, "task_runs", case.job) == before
    assert row(database, "asset_images", case.image)["status"] == "validating"


def test_renew_checks_domain_and_pinned_version(database, case):
    lease = claim(database, case)
    with transaction(database) as connection:
        assert jobs.heartbeat(connection, lease)
    update(
        database,
        "UPDATE asset_images SET original_object_version='other' WHERE id=:id",
        id=case.image,
    )
    with transaction(database) as connection:
        assert not jobs.heartbeat(connection, lease)
    assert row(database, "task_runs", case.job)["last_error_code"] == "LEASE_LOST"


@pytest.mark.parametrize("cancelled", [False, True])
def test_sweeper_abandons_attempt_and_fences_old_worker(database, case, cancelled):
    lease = claim(database, case)
    if cancelled:
        update(
            database, "UPDATE inspections SET status='cancelled' WHERE id=:id", id=case.inspection
        )
    expire(database, case)
    item_before = row(database, "inspection_items", case.item)
    discovered = candidate(database, case)
    with transaction(database) as connection:
        assert jobs.recover(connection, discovered, delay=lambda _: timedelta(seconds=5))
    task = row(database, "task_runs", case.job)
    assert task["fencing_token"] == lease.token + 1
    assert task["state"] == ("failed" if cancelled else "retry_wait")
    assert row(database, "inspection_items", case.item) == item_before
    with database.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT status FROM task_attempts WHERE task_id=:id"), {"id": case.job}
            )
            == "abandoned"
        )
    with transaction(database) as connection:
        assert not jobs.fail(connection, lease, "INTERNAL_ERROR")
        assert not jobs.recover(connection, discovered)


def test_sweeper_candidate_stale_after_renewal(database, case):
    lease = claim(database, case)
    expire(database, case)
    discovered = candidate(database, case)
    update(
        database,
        "UPDATE task_runs SET lease_until=UTC_TIMESTAMP(3)+INTERVAL 1 MINUTE WHERE id=:id",
        id=case.job,
    )
    with transaction(database) as connection:
        assert not jobs.recover(connection, discovered)
        assert jobs.heartbeat(connection, lease)


def test_four_failures_dead_letter_and_no_fifth_attempt(database, case):
    for attempt in range(1, 5):
        lease = claim(database, case)
        assert lease.attempt == attempt
        with transaction(database) as connection:
            assert jobs.fail(
                connection, lease, "DEPENDENCY_UNAVAILABLE", delay=lambda _: timedelta(seconds=5)
            )
        task = row(database, "task_runs", case.job)
        assert task["state"] == ("retry_wait" if attempt < 4 else "dead_letter")
        due(database, case)
    assert claim(database, case) is None
    with database.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM task_attempts WHERE task_id=:id"), {"id": case.job}
            )
            == 4
        )


def test_nonretryable_technical_error_stops_immediately(database, case):
    lease = claim(database, case)
    with transaction(database) as connection:
        assert jobs.fail(connection, lease, "INTERNAL_ERROR")
    assert row(database, "task_runs", case.job)["state"] == "failed"


@pytest.mark.parametrize("code", ["HASH_MISMATCH", "IMAGE_INVALID", "UNKNOWN"])
def test_content_outcomes_cannot_bypass_domain_handler(database, case, code):
    lease = claim(database, case)
    with pytest.raises(ValueError):
        with transaction(database) as connection:
            jobs.fail(connection, lease, code)
    assert row(database, "task_runs", case.job)["state"] == "leased"


def test_result_commit_is_fenced_atomic_and_duplicate_safe(database, case):
    lease = claim(database, case)
    service = ImageExecution(database)
    service.commit(lease, reject_result)
    assert row(database, "task_runs", case.job)["state"] == "succeeded"
    assert row(database, "asset_images", case.image)["status"] == "rejected"
    assert claim(database, case) is None
    with pytest.raises(LeaseLost):
        service.commit(lease, reject_result)


@pytest.mark.parametrize("failure", ["handler", "empty", "expired"])
def test_result_failure_rolls_back_all_handler_writes(database, case, failure):
    lease = claim(database, case)

    def handler(connection, input):
        if failure == "empty":
            return
        reject_result(connection, input)
        if failure == "handler":
            raise RuntimeError("handler failed")
        connection.execute(
            text(
                "UPDATE task_runs SET lease_until=UTC_TIMESTAMP(3)-INTERVAL 1 SECOND WHERE id=:id"
            ),
            {"id": case.job},
        )

    with pytest.raises((RuntimeError, LeaseLost)):
        ImageExecution(database).commit(lease, handler)
    assert row(database, "task_runs", case.job)["state"] == "leased"
    assert row(database, "asset_images", case.image)["status"] == "validating"
    assert row(database, "uploads", case.upload)["status"] == "validating"


def test_recovery_service_never_reclaims_live_lease(database, case):
    claim(database, case)
    ImageExecution(database).recover_expired()
    assert row(database, "task_runs", case.job)["state"] == "leased"
    expire(database, case)
    assert ImageExecution(database).recover_expired() >= 1
    assert row(database, "task_runs", case.job)["state"] == "retry_wait"


@pytest.mark.parametrize(
    "field,value",
    [
        ("status", "queued"),
        ("status", "processing"),
        ("status", "completed"),
    ],
)
def test_busy_owner_prevents_claim_without_business_changes(database, case, field, value):
    update(
        database,
        f"UPDATE inspection_items SET {field}=:value WHERE id=:id",
        value=value,
        id=case.item,
    )
    before = row(database, "inspection_items", case.item)
    assert claim(database, case) is None
    assert row(database, "task_runs", case.job)["last_error_code"] == "LEASE_LOST"
    assert row(database, "inspection_items", case.item) == before


@pytest.mark.parametrize("status", ["in_progress", "rejected", "closed", "pending_recheck"])
def test_remediation_owner_uses_its_own_guard(database, case, status):
    update(
        database,
        "UPDATE remediation_tasks SET status=:status WHERE id=:id",
        status=status,
        id=case.task,
    )
    for table, key in (("uploads", case.upload), ("asset_images", case.image)):
        update(
            database,
            f"UPDATE {table} SET inspection_item_id=NULL,remediation_task_id=:owner WHERE id=:id",
            owner=case.task,
            id=key,
        )
    lease = claim(database, case)
    if status in {"in_progress", "rejected"}:
        assert lease.input.owner_type == "remediation_task"
    else:
        assert lease is None


def test_concurrent_sweepers_recover_once(database, case):
    claim(database, case)
    expire(database, case)
    discovered = candidate(database, case)
    barrier = Barrier(2)

    def recover():
        barrier.wait(timeout=5)
        with transaction(database) as connection:
            return jobs.recover(connection, discovered)

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(lambda _: recover(), range(2))) == 1
    assert row(database, "task_runs", case.job)["fencing_token"] == 2


def test_kill_recovery_exhausts_four_attempts(database, case):
    for _ in range(4):
        lease = claim(database, case)
        expire(database, case)
        with transaction(database) as connection:
            assert jobs.recover(connection, candidate(database, case))
        due(database, case)
    assert row(database, "task_runs", case.job)["state"] == "dead_letter"
    with pytest.raises(LeaseLost):
        ImageExecution(database).commit(lease, reject_result)


def test_attempt_finalization_failure_rolls_back_handler(database, case):
    lease = claim(database, case)

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE task_attempts"):
            raise RuntimeError("injected attempt finalize failure")

    event.listen(database, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            ImageExecution(database).commit(lease, reject_result)
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert row(database, "task_runs", case.job)["state"] == "leased"
    assert row(database, "asset_images", case.image)["status"] == "validating"


def test_private_execute_keeps_external_work_outside_locks(database, case):
    def prepare(input, cancelled):
        assert not cancelled.is_set()
        with database.begin() as connection:
            connection.execute(
                text("SELECT id FROM asset_images WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": input.image_id},
            )
            connection.execute(
                text("SELECT id FROM task_runs WHERE id=:id FOR UPDATE NOWAIT"), {"id": case.job}
            )
        return "synthetic-rejected"

    def write(connection, input, outcome):
        assert outcome == "synthetic-rejected"
        reject_result(connection, input)

    assert ImageExecution(database).execute(case.message, prepare, write)
    assert not ImageExecution(database).execute(case.message, prepare, write)


def test_private_execute_technical_failure_preserves_image(database, case):
    def unavailable(input, cancelled):
        raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "private URL not logged")

    with pytest.raises(ServiceError):
        ImageExecution(database).execute(case.message, unavailable, None)
    assert row(database, "task_runs", case.job)["state"] == "retry_wait"
    assert row(database, "asset_images", case.image)["status"] == "validating"
