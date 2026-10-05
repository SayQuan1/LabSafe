"""Real HTTP/MySQL/Redis replay; precise object HEAD is SDK-stubbed, never live MinIO."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from redis.exceptions import ConnectionError
from sqlalchemy import event, text

from packages.application.job_execution import ImageExecution
from packages.application.jobs import JobApplication
from packages.domain.image_validation import RejectedImage
from packages.domain.job_execution import LeaseLost
from packages.persistence.image_validation import write_image_result
from packages.persistence.security import revoke_user_sessions
from packages.storage.s3 import BUCKET
from tests.persistence import test_upload_completion_mysql as completion
from tests.persistence.test_foundations_mysql import count_rows, post
from tests.persistence.test_identity_mysql import ORIGIN, assert_schema, login
from tests.persistence.test_job_execution_mysql import row, update
from tests.persistence.test_roles_mysql import command, create_user, lab_fixture, role_write
from tests.persistence.test_security_mysql import create_tenant

upload_api = completion.upload_api
completion_case = completion.completion_case


@pytest.fixture
def job_case(completion_case, database):
    case = completion_case
    with completion.head(case):
        image = completion.complete(case).json()["data"]
    case.image = image["id"]
    with database.connect() as connection:
        case.message = json.loads(
            connection.scalar(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE tenant_id=:tenant AND event_type='TaskDispatch'"
                ),
                {"tenant": case.tenant["tenant_id"]},
            )
        )
    case.job = case.message["task_id"]
    execution = ImageExecution(database)
    case.lease = execution.claim(case.message)
    assert execution.fail(case.lease, "INTERNAL_ERROR")
    case.version = row(database, "task_runs", case.job)["version"]
    case.app.state.jobs = JobApplication(case.identity, case.storage)
    yield case
    # Exclude only this disposable fixture's notices/tasks from later sweeper tests.
    with database.begin() as connection:
        for table in ("task_attempts", "outbox_events", "task_runs"):
            connection.execute(
                text(f"DELETE FROM {table} WHERE tenant_id=:tenant"),
                {"tenant": case.tenant["tenant_id"]},
            )


def replay(case, *, key=None, version=None):
    return post(
        case.http,
        case.session,
        f"/dead-letters/{case.job}/replay",
        {"expected_version": version or case.version, "reason": "Storage recovered"},
        key=key,
    )


@contextmanager
def head(case):
    with Stubber(case.storage.internal) as stub:
        stub.add_response(
            "head_object",
            {"VersionId": "pinned-v1", "ContentLength": 100, "ContentType": "image/png"},
            {"Bucket": BUCKET, "Key": case.grant["object_key"], "VersionId": "pinned-v1"},
        )
        yield
        stub.assert_no_pending_responses()


def attempts(database, case):
    with database.connect() as connection:
        return [
            dict(r)
            for r in connection.execute(
                text(
                    "SELECT * FROM task_attempts WHERE tenant_id=:tenant AND task_id=:id "
                    "ORDER BY replay_generation,attempt"
                ),
                {"tenant": case.tenant["tenant_id"], "id": case.job},
            ).mappings()
        ]


def notices(database, case):
    with database.connect() as connection:
        return [
            json.loads(r)
            for r in connection.scalars(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE tenant_id=:tenant AND aggregate_id=:id "
                    "ORDER BY created_at,id"
                ),
                {"tenant": case.tenant["tenant_id"], "id": case.job},
            )
        ]


def replay_audits(database, case):
    with database.connect() as connection:
        return list(
            connection.execute(
                text("SELECT * FROM audit_events WHERE tenant_id=:tenant AND action='job.replay'"),
                {"tenant": case.tenant["tenant_id"]},
            ).mappings()
        )


def test_job_reads_exact_public_schema_and_no_side_effects(job_case, database):
    case = job_case
    before = row(database, "task_runs", case.job)
    response = case.http.get(f"/api/v1/jobs/{case.job}")
    assert response.status_code == 200
    assert_schema(response, "Job")
    data = response.json()["data"]
    assert data["state"] == "failed" and data["last_error_code"] == "INTERNAL_ERROR"
    assert not {"payload", "fencing_token", "lease_owner", "object_key", "tenant_id"} & data.keys()
    assert row(database, "task_runs", case.job) == before
    assert replay_audits(database, case) == []


@pytest.mark.parametrize(
    "state", ["ready", "retry_wait", "leased", "succeeded", "failed", "dead_letter"]
)
def test_job_query_projects_all_persisted_states(job_case, database, state):
    case = job_case
    update(database, "UPDATE task_runs SET state=:state WHERE id=:id", id=case.job, state=state)
    response = case.http.get(f"/api/v1/jobs/{case.job}")
    assert response.status_code == 200 and response.json()["data"]["state"] == state


def test_terminal_list_filters_lab_and_excludes_other_tasks_and_tenants(job_case, database):
    case = job_case
    response = case.http.get(f"/api/v1/dead-letters?laboratory_id={case.lab['id']}&page_size=1")
    assert response.status_code == 200
    assert_schema(response, "JobPage")
    assert response.json()["data"]["total"] == 1
    assert response.json()["data"]["items"][0]["id"] == case.job
    assert case.http.get("/api/v1/dead-letters?page=2&page_size=1").json()["data"]["items"] == []
    foreign = create_tenant(database, "foreign_jobs")
    with TestClient(case.app, base_url=ORIGIN) as other:
        login(other, foreign)
        assert other.get("/api/v1/dead-letters").json()["data"]["total"] == 0
        assert other.get(f"/api/v1/jobs/{case.job}").status_code == 404
        assert other.get(f"/api/v1/dead-letters?laboratory_id={case.lab['id']}").status_code == 404
    update(
        database, "UPDATE task_runs SET task_type='inference_pipeline' WHERE id=:id", id=case.job
    )
    assert case.http.get(f"/api/v1/jobs/{case.job}").status_code == 404
    assert case.http.get("/api/v1/dead-letters").json()["data"]["total"] == 0


@pytest.mark.parametrize("role", ["viewer", "lab_manager", "inspector"])
def test_read_permission_does_not_grant_admin_replay(job_case, role):
    case = job_case
    user = create_user(case.http, case.session, "job_reader")
    assert (
        role_write(
            case.http, case.session, user["id"], command(1, role, case.lab["id"])
        ).status_code
        == 200
    )
    with TestClient(case.app, base_url=ORIGIN) as target:
        session = login(target, case.tenant, username="job_reader").json()
        assert target.get(f"/api/v1/jobs/{case.job}").status_code == 200
        assert target.get("/api/v1/dead-letters").status_code == 403
        with patch.object(case.storage, "inspect_pinned_staging") as inspect:
            response = post(
                target,
                session,
                f"/dead-letters/{case.job}/replay",
                {"expected_version": case.version, "reason": "not admin"},
            )
            assert response.status_code == 403
            inspect.assert_not_called()


@pytest.mark.parametrize("state", ["failed", "dead_letter"])
def test_replay_preserves_attempts_fences_old_generation_and_enqueues_atomically(
    job_case, database, state
):
    case = job_case
    update(database, "UPDATE task_runs SET state=:state WHERE id=:id", id=case.job, state=state)
    old = row(database, "task_runs", case.job)
    history = attempts(database, case)
    image, upload = (
        row(database, "asset_images", case.image),
        row(database, "uploads", case.grant["upload_id"]),
    )
    with head(case):
        response = replay(case)
    assert response.status_code == 202, response.text
    assert_schema(response, "Job")
    current = row(database, "task_runs", case.job)
    assert (current["state"], current["attempt"], current["replay_generation"]) == ("ready", 0, 1)
    assert current["fencing_token"] == old["fencing_token"] + 1
    assert current["dispatch_sequence"] == old["dispatch_sequence"] + 1
    assert current["version"] == old["version"] + 1
    assert current["lease_owner"] is current["lease_until"] is current["finished_at"] is None
    assert current["payload"] == old["payload"] and current["last_error_code"] is None
    assert attempts(database, case) == history
    assert row(database, "asset_images", case.image) == image
    assert row(database, "uploads", case.grant["upload_id"]) == upload
    assert len(replay_audits(database, case)) == 1
    messages = notices(database, case)
    assert len(messages) == 2
    latest = next(m for m in messages if m["replay_generation"] == 1)
    schema = json.loads(Path("contracts/task-message-v1.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(latest)
    assert ImageExecution(database).claim(case.message) is None
    assert not ImageExecution(database).heartbeat(case.lease)
    with pytest.raises(LeaseLost):
        ImageExecution(database).commit(case.lease, lambda *_: pytest.fail("old lease wrote"))
    lease = ImageExecution(database).claim(latest)
    assert (lease.generation, lease.attempt, lease.input.object_version) == (1, 1, "pinned-v1")
    assert len(attempts(database, case)) == 2 and attempts(database, case)[0] == history[0]
    ImageExecution(database).commit(
        lease,
        lambda connection, source: write_image_result(
            connection, lease.tenant_id, lease.task_id, source, RejectedImage("IMAGE_INVALID")
        ),
    )
    assert row(database, "task_runs", case.job)["state"] == "succeeded"
    assert row(database, "asset_images", case.image)["status"] == "rejected"
    assert attempts(database, case)[0] == history[0]
    assert attempts(database, case)[1]["status"] == "succeeded"


def test_same_key_replays_original_response_without_new_head_or_generation(job_case, database):
    case, key = job_case, str(uuid4())
    with head(case):
        first = replay(case, key=key)
    assert first.status_code == 202
    with patch.object(
        case.storage, "inspect_pinned_staging", side_effect=AssertionError("no repeated HEAD")
    ):
        duplicate = replay(case, key=key)
    assert duplicate.content == first.content
    assert row(database, "task_runs", case.job)["replay_generation"] == 1
    assert len(notices(database, case)) == 2 and len(replay_audits(database, case)) == 1
    assert replay(case, version=case.version).status_code == 409


@pytest.mark.parametrize(
    "kind", ["cancelled", "busy", "ready_image", "rejected_image", "wrong_payload", "version"]
)
def test_invalid_replay_source_rejects_before_object_access(job_case, database, kind):
    case = job_case
    if kind == "cancelled":
        update(
            database,
            "UPDATE inspections SET status='cancelled' WHERE id=:id",
            id=case.inspection["id"],
        )
    elif kind == "busy":
        update(
            database,
            "UPDATE inspection_items SET status='queued' WHERE id=:id",
            id=case.inspection["item_ids"][0],
        )
    elif kind.endswith("image"):
        update(
            database,
            "UPDATE asset_images SET status=:state WHERE id=:id",
            id=case.image,
            state=kind.split("_")[0],
        )
    elif kind == "wrong_payload":
        update(database, "UPDATE task_runs SET payload='{}' WHERE id=:id", id=case.job)
    with patch.object(case.storage, "inspect_pinned_staging") as inspect:
        response = replay(case, version=case.version + 1 if kind == "version" else None)
    assert response.status_code == 409
    inspect.assert_not_called()
    assert len(notices(database, case)) == 1 and replay_audits(database, case) == []


@pytest.mark.parametrize("provider,status", [("NoSuchVersion", 409), ("AccessDenied", 503)])
def test_missing_pinned_version_or_storage_error_leaves_no_pending_claim(
    job_case, database, provider, status
):
    case = job_case
    before = count_rows(database, case.tenant, "api_idempotency")
    old = row(database, "task_runs", case.job)
    with Stubber(case.storage.internal) as stub:
        stub.add_client_error(
            "head_object",
            provider,
            expected_params={
                "Bucket": BUCKET,
                "Key": case.grant["object_key"],
                "VersionId": "pinned-v1",
            },
        )
        assert replay(case).status_code == status
        stub.assert_no_pending_responses()
    assert count_rows(database, case.tenant, "api_idempotency") == before
    assert row(database, "task_runs", case.job) == old


@pytest.mark.parametrize("kind", ["session", "admin", "owner", "input", "upload"])
def test_head_outside_locks_and_commit_revalidates_mutable_inputs(job_case, database, kind):
    case = job_case

    def inspect(tenant, upload_id, pinned):
        with database.begin() as connection:
            for table, key in (("task_runs", case.job), ("asset_images", case.image)):
                connection.execute(
                    text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"), {"id": key}
                )
            assert (
                connection.scalar(
                    text(
                        "SELECT COUNT(*) FROM api_idempotency "
                        "WHERE tenant_id=:tenant AND state='pending'"
                    ),
                    {"tenant": tenant},
                )
                == 0
            )
            if kind == "session":
                revoke_user_sessions(connection, tenant, case.tenant["admin_id"])
            elif kind == "admin":
                connection.execute(
                    text("DELETE FROM user_roles WHERE tenant_id=:tenant AND user_id=:user"),
                    {"tenant": tenant, "user": case.tenant["admin_id"]},
                )
            elif kind == "owner":
                connection.execute(
                    text("UPDATE inspections SET status='cancelled' WHERE id=:id"),
                    {"id": case.inspection["id"]},
                )
            else:
                connection.execute(
                    text(
                        "UPDATE "
                        + ("uploads" if kind == "upload" else "asset_images")
                        + " SET version=version+1 WHERE id=:id"
                    ),
                    {"id": case.grant["upload_id"] if kind == "upload" else case.image},
                )
        return pinned

    with patch.object(case.storage, "inspect_pinned_staging", side_effect=inspect):
        assert replay(case).status_code == (
            401 if kind == "session" else 403 if kind == "admin" else 409
        )
    assert row(database, "task_runs", case.job)["replay_generation"] == 0
    assert replay_audits(database, case) == []


@pytest.mark.parametrize(
    "prefix",
    [
        "UPDATE task_runs",
        "INSERT INTO outbox_events",
        "INSERT INTO audit_events",
        "UPDATE api_idempotency",
    ],
)
def test_replay_write_failure_rolls_back_every_record(job_case, database, prefix):
    case = job_case
    old, history = row(database, "task_runs", case.job), attempts(database, case)
    count = count_rows(database, case.tenant, "api_idempotency")

    def fail(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(prefix):
            raise RuntimeError("injected replay failure")

    event.listen(database, "before_cursor_execute", fail)
    try:
        with head(case):
            response = replay(case)
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "INTERNAL_ERROR"
            assert "injected" not in response.text
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert row(database, "task_runs", case.job) == old and attempts(database, case) == history
    assert len(notices(database, case)) == 1 and replay_audits(database, case) == []
    assert count_rows(database, case.tenant, "api_idempotency") == count


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_replays_create_one_generation(job_case, database, same_key):
    case = job_case
    barrier = Barrier(2)
    key = str(uuid4())

    def inspect(tenant, upload_id, pinned):
        barrier.wait(timeout=10)
        return pinned

    def run(index):
        with TestClient(case.app, base_url=ORIGIN) as http:
            http.cookies.update(case.http.cookies)
            return post(
                http,
                case.session,
                f"/dead-letters/{case.job}/replay",
                {"expected_version": case.version, "reason": "Storage recovered"},
                key=key if same_key else str(uuid4()),
            )

    with patch.object(case.storage, "inspect_pinned_staging", side_effect=inspect):
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(run, range(2)))
    assert sum(r.status_code == 202 for r in responses) >= 1
    assert all(r.status_code in {202, 409} for r in responses)
    assert row(database, "task_runs", case.job)["replay_generation"] == 1
    assert len(notices(database, case)) == 2 and len(replay_audits(database, case)) == 1


def test_redis_write_failure_never_checks_storage_or_replays(job_case, database):
    case = job_case
    old = row(database, "task_runs", case.job)
    with (
        patch.object(case.identity.limits.redis, "eval", side_effect=ConnectionError("secret")),
        patch.object(case.storage, "inspect_pinned_staging") as inspect,
    ):
        assert replay(case).status_code == 503
        inspect.assert_not_called()
    assert row(database, "task_runs", case.job) == old


def test_same_key_different_body_is_rejected_without_head(job_case, database):
    case, key = job_case, str(uuid4())
    with head(case):
        assert replay(case, key=key).status_code == 202
    with patch.object(case.storage, "inspect_pinned_staging") as inspect:
        response = post(
            case.http,
            case.session,
            f"/dead-letters/{case.job}/replay",
            {"expected_version": case.version, "reason": "different request"},
            key=key,
        )
        assert response.status_code == 409
        inspect.assert_not_called()
    assert row(database, "task_runs", case.job)["replay_generation"] == 1


def test_storage_absence_rolls_back_new_claim_but_allows_completed_response(job_case, database):
    case, key = job_case, str(uuid4())
    case.app.state.jobs.storage = None
    before = count_rows(database, case.tenant, "api_idempotency")
    assert replay(case, key=key).status_code == 503
    assert count_rows(database, case.tenant, "api_idempotency") == before
    assert case.http.get(f"/api/v1/jobs/{case.job}").status_code == 200
    case.app.state.jobs.storage = case.storage
    with head(case):
        first = replay(case, key=key)
    assert first.status_code == 202
    case.app.state.jobs.storage = None
    assert replay(case, key=key).content == first.content


def test_cached_replay_rechecks_admin_after_role_revocation(job_case, database):
    case, key = job_case, str(uuid4())
    other = create_user(case.http, case.session, "remaining_admin")
    assert (
        role_write(case.http, case.session, other["id"], command(role="safety_admin")).status_code
        == 200
    )
    assert (
        role_write(
            case.http,
            case.session,
            case.tenant["admin_id"],
            command(role="viewer", lab=case.lab["id"]),
        ).status_code
        == 200
    )
    case.session = login(case.http, case.tenant).json()
    with head(case):
        assert replay(case, key=key).status_code == 202
    assert (
        role_write(
            case.http,
            case.session,
            case.tenant["admin_id"],
            command(2, "safety_admin"),
            revoke=True,
        ).status_code
        == 200
    )
    assert replay(case, key=key).status_code == 401
    case.session = login(case.http, case.tenant).json()
    with patch.object(case.storage, "inspect_pinned_staging") as inspect:
        assert replay(case, key=key).status_code == 403
        inspect.assert_not_called()
    assert row(database, "task_runs", case.job)["replay_generation"] == 1


def test_job_from_other_laboratory_is_hidden(job_case, database):
    case = job_case
    lab = lab_fixture(database, case.tenant)
    user = create_user(case.http, case.session, "other_lab_reader")
    assert (
        role_write(case.http, case.session, user["id"], command(role="viewer", lab=lab)).status_code
        == 200
    )
    with TestClient(case.app, base_url=ORIGIN) as http:
        login(http, case.tenant, username="other_lab_reader")
        assert http.get(f"/api/v1/jobs/{case.job}").status_code == 404


def test_dead_letter_count_and_items_share_snapshot_during_concurrent_replay(job_case, database):
    case = job_case
    changed = False

    def change_after_count(_conn, _cursor, statement, _params, _context, _many):
        nonlocal changed
        if not changed and statement.startswith("SELECT COUNT(*) FROM task_runs job"):
            changed = True
            update(database, "UPDATE task_runs SET state='ready' WHERE id=:id", id=case.job)

    event.listen(database, "after_cursor_execute", change_after_count)
    try:
        response = case.http.get("/api/v1/dead-letters")
    finally:
        event.remove(database, "after_cursor_execute", change_after_count)
    assert changed and response.status_code == 200
    assert response.json()["data"]["total"] == 1
    assert response.json()["data"]["items"][0]["state"] == "failed"
    assert case.http.get("/api/v1/dead-letters").json()["data"]["total"] == 0
