"""CSV execution races, fences and transaction failures on disposable MySQL."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from packages.application.dispatch import DispatchPublisher, transaction
from packages.application.report_execution import ReportExecution
from packages.domain.job_execution import LeaseLost
from packages.domain.job_replay import require_replay
from packages.domain.report_execution import ReportInvalid, artifact_for, csv_bytes
from packages.persistence import dispatch
from packages.persistence import report_execution as execution
from packages.persistence.dispatch import decoded
from packages.persistence.jobs import JobRepository
from tests.persistence.factories import Graph
from tests.persistence.test_report_export_mysql import admin, body, create


@pytest.fixture
def case(database):
    with database.begin() as connection:
        graph = Graph(connection)
        for item, run, fact, evaluation in (
            (graph.item, graph.run, graph.fact, graph.evaluation),
            (graph.other_item, graph.other_run, graph.other_fact, graph.other_evaluation),
        ):
            connection.execute(
                text(
                    "UPDATE inspection_items SET current_run_id=:run,"
                    "current_fact_revision_id=:fact,"
                    "current_evaluation_id=:evaluation WHERE id=:id"
                ),
                {"run": run, "fact": fact, "evaluation": evaluation, "id": item},
            )
        result = create(connection, graph)
        graph.export, graph.job = result["id"], result["job_id"]
        graph.message = decoded(
            connection.scalar(
                text(
                    "SELECT payload FROM outbox_events WHERE aggregate_id=:job "
                    "AND event_type='TaskDispatch'"
                ),
                {"job": graph.job},
            )
        )
    yield graph
    # Only retire this fixture's notices/tasks; keep its business graph in the disposable DB.
    with database.begin() as connection:
        for name in ("task_attempts", "outbox_events", "task_runs"):
            connection.execute(
                text(f"DELETE FROM {name} WHERE tenant_id=:tenant"), {"tenant": graph.tenant}
            )


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


def claim(database, case, message=None):
    with transaction(database) as connection:
        return execution.claim(connection, message or case.message, str(uuid4()))


def records(database, case):
    with database.connect() as connection:
        return {
            name: [
                dict(r)
                for r in connection.execute(
                    text(f"SELECT * FROM {name} WHERE tenant_id=:tenant ORDER BY id"),
                    {"tenant": case.tenant},
                ).mappings()
            ]
            for name in (
                "report_exports",
                "task_runs",
                "task_attempts",
                "outbox_events",
                "audit_events",
            )
        }


def artifact(lease):
    return artifact_for(lease.input, csv_bytes(lease.input), "test-exact-version")


def test_report_execution_success_pins_artifact_and_duplicate_is_noop(database, case):
    lease = claim(database, case)
    assert lease.attempt == lease.token == 1
    assert row(database, "report_exports", case.export)["status"] == "running"
    assert claim(database, case) is None
    with transaction(database) as connection:
        assert execution.heartbeat(connection, lease)
        assert execution.commit_result(connection, lease, artifact(lease))
    result, job = row(database, "report_exports", case.export), row(database, "task_runs", case.job)
    assert result["status"] == "ready" and job["state"] == "succeeded"
    assert result["object_version"] == "test-exact-version"
    assert result["checksum"] == artifact(lease).checksum
    assert result["object_key"] == artifact(lease).key
    assert result["size_bytes"] == len(csv_bytes(lease.input))
    assert result["expires_at"] - job["finished_at"] == timedelta(hours=24)
    saved = records(database, case)
    assert saved["task_attempts"][0]["status"] == "succeeded"
    ready_audit = [r for r in saved["audit_events"] if r["action"] == "report.export_ready"]
    assert len(ready_audit) == 1 and ready_audit[0]["actor_id"] is None
    assert {r["event_type"] for r in saved["outbox_events"]} == {"TaskDispatch", "ReportRequested"}
    assert claim(database, case) is None
    with transaction(database) as connection:
        assert not execution.fail(connection, lease, "INTERNAL_ERROR")
        with pytest.raises(LeaseLost):
            execution.commit_result(connection, lease, artifact(lease))
    assert records(database, case) == saved


def test_report_execution_concurrent_claim_has_one_attempt(database, case):
    barrier = Barrier(2)

    def run(_):
        barrier.wait(timeout=5)
        return claim(database, case)

    with ThreadPoolExecutor(max_workers=2) as pool:
        leases = list(pool.map(run, range(2)))
    assert sum(lease is not None for lease in leases) == 1
    assert len(records(database, case)["task_attempts"]) == 1


@pytest.mark.parametrize("counter", ["replay_generation", "dispatch_sequence"])
def test_report_execution_old_and_future_notifications(database, case, counter):
    update(database, f"UPDATE task_runs SET {counter}={counter}+1 WHERE id=:id", id=case.job)
    before = records(database, case)
    assert claim(database, case) is None
    with pytest.raises(ValueError, match="Future"):
        claim(database, case, {**case.message, counter: case.message[counter] + 2})
    assert records(database, case) == before
    assert claim(database, case, {**case.message, counter: case.message[counter] + 1}) is not None


@pytest.mark.parametrize("fault", ["tenant", "resource", "pdf", "not_due"])
def test_report_execution_claim_guards(database, case, fault):
    message = dict(case.message)
    if fault == "tenant":
        message["tenant_id"] = str(uuid4())
    elif fault == "resource":
        message["resource_id"] = str(uuid4())
        message["payload"] = {"export_id": message["resource_id"]}
    elif fault == "pdf":
        update(database, "UPDATE report_exports SET format='pdf' WHERE id=:id", id=case.export)
    else:
        update(
            database,
            "UPDATE task_runs SET available_at=UTC_TIMESTAMP(3)+INTERVAL 1 DAY WHERE id=:id",
            id=case.job,
        )
    before = records(database, case)
    if fault == "resource":
        with pytest.raises(ValueError):
            claim(database, case, message)
    else:
        assert claim(database, case, message) is None
    assert records(database, case) == before


@pytest.mark.parametrize(
    "fault",
    ["owner", "token", "generation", "attempt", "snapshot", "filters", "version", "expired"],
)
def test_report_execution_changed_ownership_or_input_rejects_all_writes(database, case, fault):
    lease = claim(database, case)
    submitted = lease
    if fault in {"owner", "token", "generation", "attempt"}:
        submitted = replace(
            lease, **{fault: str(uuid4()) if fault == "owner" else getattr(lease, fault) + 1}
        )
    elif fault == "snapshot":
        update(
            database,
            "UPDATE report_exports SET snapshot=JSON_SET(snapshot,'$.rows',JSON_ARRAY()) "
            "WHERE id=:id",
            id=case.export,
        )
    elif fault == "filters":
        update(
            database,
            "UPDATE report_exports SET filters=JSON_SET(filters,'$.severity',JSON_ARRAY('high')) "
            "WHERE id=:id",
            id=case.export,
        )
    elif fault == "version":
        update(database, "UPDATE report_exports SET version=version+1 WHERE id=:id", id=case.export)
    else:
        update(
            database,
            "UPDATE task_runs SET lease_until=UTC_TIMESTAMP(3)-INTERVAL 1 SECOND WHERE id=:id",
            id=case.job,
        )
    before = records(database, case)
    with transaction(database) as connection:
        assert not execution.heartbeat(connection, submitted)
        assert not execution.fail(connection, submitted, "INTERNAL_ERROR")
        with pytest.raises(LeaseLost):
            execution.commit_result(connection, submitted, artifact(lease))
    assert records(database, case) == before


@pytest.mark.parametrize(
    "code,expected",
    [
        ("DEPENDENCY_UNAVAILABLE", "retry_wait"),
        ("STAGE_TIMEOUT", "retry_wait"),
        ("SCHEMA_MISMATCH", "failed"),
        ("INTERNAL_ERROR", "failed"),
    ],
)
def test_report_execution_failure_converges_report_task_and_attempt(database, case, code, expected):
    lease = claim(database, case)
    with transaction(database) as connection:
        assert execution.fail(connection, lease, code)
    result, job = row(database, "report_exports", case.export), row(database, "task_runs", case.job)
    assert job["state"] == expected and job["fencing_token"] > lease.token
    assert result["status"] == ("queued" if expected == "retry_wait" else "failed")
    assert result["last_error_code"] == job["last_error_code"] == code
    assert result["object_key"] is result["object_version"] is None
    attempt = records(database, case)["task_attempts"][0]
    assert attempt["status"] == "failed" and attempt["error_code"] == code
    if expected == "retry_wait":
        delay = (job["available_at"] - attempt["finished_at"]).total_seconds()
        assert 5 <= delay <= 6


def test_report_execution_four_expirations_dead_letter_and_old_lease_is_fenced(database, case):
    old = None
    for attempt in range(1, 5):
        update(
            database, "UPDATE task_runs SET available_at=UTC_TIMESTAMP(3) WHERE id=:id", id=case.job
        )
        lease = claim(database, case)
        assert lease.attempt == attempt
        old = old or lease
        update(
            database,
            "UPDATE task_runs SET lease_until=UTC_TIMESTAMP(3)-INTERVAL 1 SECOND WHERE id=:id",
            id=case.job,
        )
        with transaction(database) as connection:
            candidates = execution.expired_candidates(connection)
            candidate = next(r for r in candidates if r["id"] == case.job)
            assert execution.recover(connection, candidate)
            assert not execution.recover(connection, candidate)
        job = row(database, "task_runs", case.job)
        assert job["state"] == ("retry_wait" if attempt < 4 else "dead_letter")
        assert row(database, "report_exports", case.export)["status"] == (
            "queued" if attempt < 4 else "failed"
        )
    before = records(database, case)
    assert len(before["task_attempts"]) == 4
    assert all(r["status"] == "abandoned" for r in before["task_attempts"])
    with transaction(database) as connection:
        assert not execution.fail(connection, old, "INTERNAL_ERROR")
        with pytest.raises(LeaseLost):
            execution.commit_result(connection, old, artifact(old))
    assert records(database, case) == before


@pytest.mark.parametrize(
    "phase,statement",
    [
        ("claim", "UPDATE task_runs"),
        ("claim", "INSERT INTO task_attempts"),
        ("claim", "UPDATE report_exports"),
        ("commit", "UPDATE report_exports"),
        ("commit", "UPDATE task_attempts"),
        ("commit", "INSERT INTO audit_events"),
        ("commit", "UPDATE task_runs"),
        ("fail", "UPDATE task_attempts"),
        ("fail", "UPDATE task_runs"),
        ("fail", "UPDATE report_exports"),
    ],
)
def test_report_execution_write_failures_roll_back_complete_write_set(
    database, case, phase, statement
):
    lease = None if phase == "claim" else claim(database, case)
    before = records(database, case)

    def fail(connection, cursor, sql, params, context, many):
        if sql.startswith(statement):
            raise RuntimeError("injected report execution write failure")

    event.listen(database, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            with transaction(database) as connection:
                if phase == "claim":
                    execution.claim(connection, case.message, str(uuid4()))
                elif phase == "commit":
                    execution.commit_result(connection, lease, artifact(lease))
                else:
                    execution.fail(connection, lease, "DEPENDENCY_UNAVAILABLE")
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert records(database, case) == before


def test_report_execution_prepare_uses_frozen_data_outside_database_locks(database, case):
    update(
        database,
        "UPDATE findings SET explanation='changed after snapshot' WHERE id=:id",
        id=case.finding,
    )
    service = ReportExecution(database)

    def prepare(source, cancelled):
        # An independent connection can immediately lock both rows while external I/O runs.
        with transaction(database) as connection:
            connection.execute(
                text("SELECT id FROM report_exports WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": case.export},
            )
            connection.execute(
                text("SELECT id FROM task_runs WHERE id=:id FOR UPDATE NOWAIT"), {"id": case.job}
            )
        payload = csv_bytes(source)
        assert b"changed after snapshot" not in payload
        return artifact_for(source, payload, "prepared-outside-db")

    assert service.execute(case.message, prepare)
    assert row(database, "report_exports", case.export)["object_version"] == "prepared-outside-db"


def test_report_execution_invalid_snapshot_becomes_permanent_failure(database, case):
    update(database, "UPDATE report_exports SET snapshot='{}' WHERE id=:id", id=case.export)
    with pytest.raises(ReportInvalid):
        ReportExecution(database).execute(case.message, lambda source, cancelled: csv_bytes(source))
    assert row(database, "report_exports", case.export)["last_error_code"] == "SCHEMA_MISMATCH"
    assert row(database, "task_runs", case.job)["state"] == "failed"


def test_report_execution_publish_and_redispatch_gate_csv_only(database, case):
    with database.begin() as connection:
        pdf = create(connection, case, {**body([case.lab]), "format": "pdf"})
        connection.execute(
            text("UPDATE outbox_events SET available_at='1990-01-01' WHERE tenant_id=:tenant"),
            {"tenant": case.tenant},
        )
    transport = Mock()
    DispatchPublisher(database, transport).publish_batch(limit=1)
    assert not any(
        call.args[1].get("tenant_id") == case.tenant for call in transport.publish.call_args_list
    )
    transport.reset_mock()
    assert DispatchPublisher(database, transport, report_enabled=True).publish_batch(limit=1) == 1
    assert transport.publish.call_args.args[1]["task_id"] == case.job
    with transaction(database) as connection:
        connection.execute(
            text(
                "UPDATE task_runs SET last_dispatched_at=NULL,available_at='1990-01-01' "
                "WHERE tenant_id=:tenant"
            ),
            {"tenant": case.tenant},
        )
        dispatch.redispatch(connection, report_enabled=False)
        assert (
            connection.scalar(
                text("SELECT dispatch_sequence FROM task_runs WHERE id=:id"), {"id": case.job}
            )
            == 1
        )
        dispatch.redispatch(connection, report_enabled=True)
    assert row(database, "task_runs", case.job)["dispatch_sequence"] == 2
    assert row(database, "task_runs", pdf["job_id"])["dispatch_sequence"] == 1
    assert row(database, "report_exports", pdf["id"])["status"] == "queued"
    assert claim(database, case) is None  # The old notification was superseded.
    assert claim(database, case, {**case.message, "dispatch_sequence": 2}) is not None


def test_report_execution_expired_publication_returns_to_pending(database, case):
    update(
        database,
        "UPDATE outbox_events SET state='leased',lease_owner=:owner,lease_until='1990-01-01' "
        "WHERE tenant_id=:tenant AND event_type='TaskDispatch'",
        owner=str(uuid4()),
        tenant=case.tenant,
    )
    with transaction(database) as connection:
        dispatch.recover_publications(connection, report_enabled=False)
        assert (
            connection.scalar(
                text(
                    "SELECT state FROM outbox_events WHERE aggregate_id=:id "
                    "AND event_type='TaskDispatch'"
                ),
                {"id": case.job},
            )
            == "leased"
        )
        dispatch.recover_publications(connection, report_enabled=True)
        assert (
            connection.scalar(
                text(
                    "SELECT state FROM outbox_events WHERE aggregate_id=:id "
                    "AND event_type='TaskDispatch'"
                ),
                {"id": case.job},
            )
            == "pending"
        )


def test_report_execution_failed_attempt_replay_preserves_snapshot_and_history(database, case):
    old = claim(database, case)
    with transaction(database) as connection:
        assert execution.fail(connection, old, "INTERNAL_ERROR")
    before = records(database, case)
    repository = JobRepository()
    actor = admin(case)
    with transaction(database) as connection:
        task, report = repository.replay_source(connection, actor, case.job)
        require_replay(actor, task, report, task["version"])
        result = repository.replay(connection, actor, task, "Retry frozen CSV", str(uuid4()))
    assert result["state"] == "ready" and result["replay_generation"] == 1
    assert claim(database, case) is None
    with transaction(database) as connection:
        assert not execution.fail(connection, old, "INTERNAL_ERROR")
        with pytest.raises(LeaseLost):
            execution.commit_result(connection, old, artifact(old))
    new = claim(database, case, {**case.message, "replay_generation": 1, "dispatch_sequence": 2})
    assert new.attempt == 1 and new.token > old.token
    assert new.input.snapshot == old.input.snapshot
    with transaction(database) as connection:
        execution.commit_result(connection, new, artifact(new))
    after = records(database, case)
    assert all(entry in after["task_attempts"] for entry in before["task_attempts"])
    assert len(after["task_attempts"]) == 2
    assert row(database, "report_exports", case.export)["status"] == "ready"


def test_report_execution_recovered_winner_cannot_be_overwritten_by_old_upload(database, case):
    old = claim(database, case)
    orphan = artifact(old)
    update(database, "UPDATE task_runs SET lease_until='1990-01-01' WHERE id=:id", id=case.job)
    with transaction(database) as connection:
        candidate = next(r for r in execution.expired_candidates(connection) if r["id"] == case.job)
        assert execution.recover(connection, candidate)
    update(database, "UPDATE task_runs SET available_at=UTC_TIMESTAMP(3) WHERE id=:id", id=case.job)
    winner = claim(database, case)
    with transaction(database) as connection:
        execution.commit_result(
            connection, winner, replace(artifact(winner), object_version="winner")
        )
    before = records(database, case)
    with transaction(database) as connection:
        with pytest.raises(LeaseLost):
            execution.commit_result(connection, old, orphan)
        assert not execution.fail(connection, old, "INTERNAL_ERROR")
    assert records(database, case) == before
    assert row(database, "report_exports", case.export)["object_version"] == "winner"
