"""Real MySQL scheduling races and real Redis/Celery transport recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from apps.worker.app.main import create_celery_app
from apps.worker.dispatch import CeleryDispatchTransport
from packages.application.dispatch import DispatchPublisher, sweep_dispatch
from packages.persistence import dispatch
from packages.persistence.image_jobs import enqueue_image_validation
from tests.persistence.factories import insert


@pytest.fixture
def database(database):
    # Scheduler connections use RC; API/query tests retain their normal RR engine.
    return database.execution_options(isolation_level="READ COMMITTED")


@pytest.fixture
def intent(database):
    tenant = str(uuid4())
    with database.begin() as connection:
        insert(connection, "tenants", id=tenant, timezone="UTC")
        task = enqueue_image_validation(
            connection,
            tenant_id=tenant,
            upload_id=str(uuid4()),
            image_id=str(uuid4()),
            request_id=str(uuid4()),
        )
        # Deterministic first candidate without depending on other suite fixtures.
        connection.execute(
            text("UPDATE outbox_events SET available_at='2000-01-01' WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
    yield tenant, task
    with database.begin() as connection:
        connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id=:tenant"), {"tenant": tenant}
        )
        connection.execute(
            text("DELETE FROM task_runs WHERE tenant_id=:tenant"), {"tenant": tenant}
        )
        connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant})


def rows(database, intent):
    with database.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM outbox_events WHERE tenant_id=:tenant ORDER BY created_at,id"),
                {"tenant": intent[0]},
            ).mappings()
        ]


def task_row(database, intent):
    with database.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT * FROM task_runs WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": intent[0], "id": intent[1]},
            )
            .mappings()
            .one()
        )


def expire(database, intent):
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE outbox_events SET lease_until=UTC_TIMESTAMP(3)-"
                "INTERVAL 1 SECOND WHERE tenant_id=:tenant AND state='leased'"
            ),
            {"tenant": intent[0]},
        )


def test_publish_after_transaction_and_stable_broker_id(database, intent):
    transport = Mock()

    def publish(event_id, message, renew):
        current = rows(database, intent)[0]
        assert current["state"] == "leased" and current["id"] == event_id
        assert message["task_id"] == intent[1]
        # Prove broker callback can acquire the row lock from a separate connection.
        with database.begin() as connection:
            connection.execute(
                text("SELECT id FROM outbox_events WHERE id=:id FOR UPDATE NOWAIT"),
                {"id": event_id},
            )
        renew()

    transport.publish.side_effect = publish
    assert DispatchPublisher(database, transport).publish_batch(1) == 1
    row = rows(database, intent)[0]
    assert row["state"] == "published" and row["published_at"] is not None
    assert row["lease_owner"] is row["lease_until"] is None
    assert task_row(database, intent)["state"] == "ready"


def test_publish_failure_releases_with_backoff_without_secret_log(database, intent, caplog):
    transport = Mock()
    transport.publish.side_effect = RuntimeError("redis://private-secret")
    assert DispatchPublisher(database, transport).publish_batch(1) == 0
    row = rows(database, intent)[0]
    assert row["state"] == "pending" and row["attempts"] == 1
    assert row["available_at"] > row["updated_at"]
    assert "private-secret" not in caplog.text


def test_process_crash_after_send_republishes_same_event(database, intent):
    owner = str(uuid4())
    with database.begin() as connection:
        row = dispatch.claim(connection, owner)
    assert row["tenant_id"] == intent[0]
    # Represents broker success followed by publisher kill, before mark-published.
    expire(database, intent)
    with database.begin() as connection:
        assert dispatch.recover_publications(connection) >= 1
    transport = Mock()
    assert DispatchPublisher(database, transport).publish_batch(1) == 1
    assert transport.publish.call_args.args[0] == row["id"]
    assert rows(database, intent)[0]["attempts"] == 2


@pytest.mark.parametrize("operation", ["renew", "published", "release"])
@pytest.mark.parametrize("reason", ["expired", "other_owner", "reclaimed"])
def test_stale_publisher_cannot_mutate_row(database, intent, operation, reason):
    owner = str(uuid4())
    with database.begin() as connection:
        row = dispatch.claim(connection, owner)
    if reason != "other_owner":
        expire(database, intent)
    if reason == "reclaimed":
        with database.begin() as connection:
            dispatch.recover_publications(connection)
            dispatch.claim(connection, str(uuid4()))
    before = rows(database, intent)
    with database.begin() as connection:
        assert not dispatch.owned_update(
            connection, row, str(uuid4()) if reason == "other_owner" else owner, operation
        )
    assert rows(database, intent) == before


def test_two_publishers_do_not_share_a_live_lease(database, intent):
    barrier = Barrier(2)

    def claim():
        with database.begin() as connection:
            result = dispatch.claim(connection, str(uuid4()))
            barrier.wait(timeout=10)
            return result

    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))
    assert sum(row is not None and row["tenant_id"] == intent[0] for row in claims) == 1
    assert rows(database, intent)[0]["attempts"] == 1


def test_sweeper_atomic_sequence_and_rate_limit(database, intent):
    sweep_dispatch(database)
    first = task_row(database, intent)
    assert first["dispatch_sequence"] == 2 and first["last_dispatched_at"] is not None
    assert len(rows(database, intent)) == 2
    sweep_dispatch(database)
    assert task_row(database, intent) == first
    assert len(rows(database, intent)) == 2
    last = [
        json.loads(r["payload"])
        for r in rows(database, intent)
        if json.loads(r["payload"])["dispatch_sequence"] == 2
    ][0]
    assert last["payload"] == json.loads(first["payload"])
    assert last["replay_generation"] == first["replay_generation"]


@pytest.mark.parametrize("state", ["leased", "succeeded", "failed", "dead_letter"])
def test_sweeper_skips_non_dispatchable_tasks(database, intent, state):
    with database.begin() as connection:
        connection.execute(
            text("UPDATE task_runs SET state=:state WHERE id=:id"),
            {"state": state, "id": intent[1]},
        )
    sweep_dispatch(database)
    assert task_row(database, intent)["dispatch_sequence"] == 1
    assert len(rows(database, intent)) == 1


def test_sweeper_skips_future_retry(database, intent):
    with database.begin() as connection:
        connection.execute(
            text(
                "UPDATE task_runs SET state='retry_wait',available_at="
                "UTC_TIMESTAMP(3)+INTERVAL 1 DAY WHERE id=:id"
            ),
            {"id": intent[1]},
        )
    sweep_dispatch(database)
    assert task_row(database, intent)["dispatch_sequence"] == 1


def test_sweeper_concurrency_registers_only_one_new_intent(database, intent):
    barrier = Barrier(2)

    def sweep():
        with database.begin() as connection:
            dispatch.redispatch(connection)
            barrier.wait(timeout=10)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: sweep(), range(2)))
    assert task_row(database, intent)["dispatch_sequence"] == 2
    assert len(rows(database, intent)) == 2


def test_sweeper_outbox_insert_failure_rolls_back_sequence(database, intent):
    before = task_row(database, intent)

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO outbox_events"):
            raise RuntimeError("injected")

    event.listen(database, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            sweep_dispatch(database)
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert task_row(database, intent) == before
    assert len(rows(database, intent)) == 1


@pytest.mark.parametrize("kind", ["version", "tenant", "aggregate", "payload"])
def test_invalid_outbox_not_sent(database, intent, kind):
    row = rows(database, intent)[0]
    message = json.loads(row["payload"])
    if kind == "version":
        message["schema_version"] = "unknown"
    elif kind == "tenant":
        message["tenant_id"] = str(uuid4())
    elif kind == "aggregate":
        message["task_id"] = str(uuid4())
    else:
        message["payload"]["object_key"] = "injected"
    with database.begin() as connection:
        connection.execute(
            text("UPDATE outbox_events SET payload=:payload WHERE id=:id"),
            {"id": row["id"], "payload": json.dumps(message)},
        )
    transport = Mock()
    assert DispatchPublisher(database, transport).publish_batch(1) == 0
    transport.publish.assert_not_called()
    assert rows(database, intent)[0]["state"] == "pending"


def test_real_redis_loss_is_repaired_from_published_intent(
    database, intent, isolated_redis, monkeypatch
):
    port = isolated_redis.connection_pool.connection_kwargs["port"]
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{port}/0")
    publisher = DispatchPublisher(database, CeleryDispatchTransport())
    app = create_celery_app()
    with app.connection_for_read() as connection:
        queue = connection.SimpleQueue("q.general")
        try:
            assert publisher.publish_batch(1) == 1
            notice = queue.get(block=True, timeout=5)
            assert notice.payload[0][0]["task_id"] == intent[1]
            assert notice.headers["id"] == rows(database, intent)[0]["id"]
            notice.ack()  # Removes this notice, simulating its loss before execution.
            # Redis removes the list key when empty; passive queue_declare then
            # returns NOT_FOUND even though deletion/ack succeeded.
            assert isolated_redis.llen("q.general") == 0
            assert rows(database, intent)[0]["state"] == "published"
            sweep_dispatch(database)
            assert publisher.publish_batch(1) == 1
            replacement = queue.get(block=True, timeout=5)
            assert replacement.payload[0][0]["task_id"] == intent[1]
            assert replacement.payload[0][0]["dispatch_sequence"] == 2
            replacement.ack()
            assert task_row(database, intent)["state"] == "ready"
        finally:
            queue.close()
    app.close()
