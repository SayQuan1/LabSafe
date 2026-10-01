"""Real MySQL/Redis acceptance; object responses remain explicitly stubbed, not live S3."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from redis.exceptions import ConnectionError
from sqlalchemy import event, text

from packages.persistence.security import utc_now
from packages.storage.s3 import BUCKET, ObjectVersion
from tests.domain.test_uploads import upload_body
from tests.persistence import test_upload_grants_mysql as grants
from tests.persistence.factories import insert
from tests.persistence.test_foundations_mysql import count_rows, post
from tests.persistence.test_identity_mysql import ORIGIN, assert_schema, login
from tests.persistence.test_item_queries_mysql import inspection
from tests.persistence.test_roles_mysql import command, create_user, role_write
from tests.persistence.test_security_mysql import create_tenant

upload_api = grants.upload_api
counts = grants.counts


@pytest.fixture
def completion_case(upload_api):
    tenant, identity, app, storage = upload_api
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, lab, _, _ = inspection(http, session)
        request = upload_body(value["item_ids"][0])
        granted = post(http, session, "/uploads", request)
        assert granted.status_code == 201, granted.text
        grant = granted.json()["data"]
        yield SimpleNamespace(
            tenant=tenant,
            identity=identity,
            app=app,
            storage=storage,
            http=http,
            session=session,
            inspection=value,
            lab=lab,
            request=request,
            grant=grant,
            body={"upload_id": grant["upload_id"], "sha256": request["sha256"]},
        )


@contextmanager
def head(case, **overrides):
    with Stubber(case.storage.internal) as stub:
        stub.add_response(
            "head_object",
            {
                "VersionId": "pinned-v1",
                "ContentLength": 100,
                "ContentType": "image/png",
                **overrides,
            },
            {"Bucket": BUCKET, "Key": case.grant["object_key"]},
        )
        yield
        stub.assert_no_pending_responses()


def complete(case, *, key=None):
    return post(case.http, case.session, "/uploads/complete", case.body, key=key)


def pinned(case):
    return ObjectVersion(case.grant["object_key"], "pinned-v1", 100, "image/png")


def update(database, statement, **params):
    with database.begin() as connection:
        connection.execute(text(statement), params)


def rows(database, case, table):
    with database.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(f"SELECT * FROM {table} WHERE tenant_id=:tenant"),
                {"tenant": case.tenant["tenant_id"]},
            ).mappings()
        ]


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def test_completion_registers_one_image_task_dispatch_and_exact_public_projection(
    completion_case, database
):
    case = completion_case
    before = counts(database, case.tenant)
    with head(case, Metadata={"sha256": "untrusted-head-metadata"}):
        response = complete(case)
    assert response.status_code == 202, response.text
    assert_schema(response, "Image")
    image = response.json()["data"]
    assert image["status"] == "validating" and image["version"] == 1
    assert (
        image["owner_id"] == case.request["owner_id"] and image["owner_type"] == "inspection_item"
    )
    assert image["analysis_sha256"] is image["width"] is image["height"] is None
    after = counts(database, case.tenant)
    assert {name: after[name] - before[name] for name in before} == {
        "uploads": 0,
        "asset_images": 1,
        "task_runs": 1,
        "outbox_events": 1,
        "api_idempotency": 1,
        "audit_events": 1,
    }
    stored = rows(database, case, "asset_images")[0]
    assert stored["original_key"] == case.grant["object_key"]
    assert stored["original_object_version"] == "pinned-v1"
    assert stored["original_sha256"] == case.body["sha256"]
    assert stored["analysis_key"] is stored["analysis_object_version"] is None
    upload = rows(database, case, "uploads")[0]
    assert upload["status"] == "validating" and upload["version"] == 2
    task = rows(database, case, "task_runs")[0]
    assert task["logical_key"] == f"validate_image:{image['id']}"
    assert (
        task["state"] == "ready"
        and task["attempt"] == task["fencing_token"] == task["replay_generation"] == 0
    )
    assert task["dispatch_sequence"] == 1 and task["last_dispatched_at"] is None
    assert task["lease_owner"] is task["lease_until"] is None
    outbox = rows(database, case, "outbox_events")[0]
    assert outbox["event_type"] == "TaskDispatch" and outbox["state"] == "pending"
    assert outbox["aggregate_type"] == "task_run" and outbox["aggregate_id"] == task["id"]
    message = decoded(outbox["payload"])
    spec = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/task-message-v1.json").read_text()
    )
    Draft202012Validator(spec, format_checker=FormatChecker()).validate(message)
    assert message["task_id"] == task["id"] and message["resource_id"] == image["id"]
    assert message["trace_id"] == response.json()["request_id"].replace("-", "")
    assert (
        message["payload"]
        == decoded(task["payload"])
        == {"upload_id": case.body["upload_id"], "image_id": image["id"]}
    )
    before_get = counts(database, case.tenant)
    with patch.object(
        case.storage, "inspect_staging", side_effect=AssertionError("GET must not HEAD")
    ):
        queried = case.http.get(f"/api/v1/images/{image['id']}")
    assert queried.status_code == 200 and queried.json()["data"] == image
    assert queried.headers["cache-control"] == "no-store"
    assert counts(database, case.tenant) == before_get
    item = case.http.get(f"/api/v1/inspection-items/{case.request['owner_id']}").json()["data"]
    assert item["status"] == "draft" and item["version"] == 1 and item["allowed_actions"] == []


def test_same_key_replays_original_but_new_key_reads_existing_image_without_head(
    completion_case, database
):
    case, key = completion_case, str(uuid4())
    with head(case):
        first = complete(case, key=key)
    assert first.status_code == 202
    image_id = first.json()["data"]["id"]
    # Synthetic later history: there is no worker in this batch.
    update(
        database, "UPDATE asset_images SET status='rejected',version=2 WHERE id=:id", id=image_id
    )
    update(
        database,
        "UPDATE uploads SET status='rejected',expires_at=:expired WHERE id=:id",
        id=case.body["upload_id"],
        expired=utc_now() - timedelta(hours=1),
    )
    update(
        database,
        "UPDATE inspection_items SET status='completed' WHERE id=:id",
        id=case.request["owner_id"],
    )
    before = counts(database, case.tenant)
    with patch.object(
        case.storage, "inspect_staging", side_effect=AssertionError("Never inspect latest again")
    ):
        replay = complete(case, key=key)
        assert replay.status_code == 202 and replay.content == first.content
        again = complete(case)
        assert again.status_code == 202 and again.json()["data"]["id"] == image_id
        assert again.json()["data"]["status"] == "rejected"
        conflict = post(
            case.http, case.session, "/uploads/complete", {**case.body, "sha256": "b" * 64}, key=key
        )
        assert (
            conflict.status_code == 409
            and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        )
        mismatch = post(
            case.http, case.session, "/uploads/complete", {**case.body, "sha256": "b" * 64}
        )
        assert mismatch.status_code == 422
    after = counts(database, case.tenant)
    assert after == {**before, "api_idempotency": before["api_idempotency"] + 1}
    assert rows(database, case, "asset_images")[0]["original_object_version"] == "pinned-v1"


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"ContentLength": 99}, 422),
        ({"ContentType": "image/jpeg"}, 422),
        ({"ContentLength": 15728641}, 413),
        ({"VersionId": "null"}, 503),
    ],
)
def test_actual_head_metadata_failure_has_no_database_side_effects(
    completion_case, database, metadata, expected
):
    case = completion_case
    before = counts(database, case.tenant)
    with head(case, **metadata):
        response = complete(case)
    assert response.status_code == expected, response.text
    assert counts(database, case.tenant) == before
    assert rows(database, case, "uploads")[0]["status"] == "granted"


@pytest.mark.parametrize("code,expected", [("NoSuchKey", 404), ("AccessDenied", 503)])
def test_head_errors_do_not_leave_a_persisted_pending_claim(
    completion_case, database, code, expected
):
    case = completion_case
    before = counts(database, case.tenant)
    with Stubber(case.storage.internal) as stub:
        stub.add_client_error(
            "head_object",
            code,
            "private provider message",
            expected_params={"Bucket": BUCKET, "Key": case.grant["object_key"]},
        )
        response = complete(case)
    assert response.status_code == expected and "private" not in response.text
    assert counts(database, case.tenant) == before


@pytest.mark.parametrize("failure", ["hash", "expired", "closed", "missing"])
def test_completion_rejects_before_head(completion_case, database, failure):
    case = completion_case
    if failure == "hash":
        case.body["sha256"] = "b" * 64
    elif failure == "expired":
        update(
            database,
            "UPDATE uploads SET expires_at=:expired WHERE id=:id",
            expired=utc_now(),
            id=case.body["upload_id"],
        )
    elif failure == "closed":
        update(
            database,
            "UPDATE inspections SET status='cancelled' WHERE id=:id",
            id=case.inspection["id"],
        )
    else:
        case.body["upload_id"] = str(uuid4())
    before = counts(database, case.tenant)
    with patch.object(
        case.storage, "inspect_staging", side_effect=AssertionError("Unauthorized HEAD")
    ):
        response = complete(case)
    assert (
        response.status_code
        == {"hash": 422, "expired": 409, "closed": 409, "missing": 404}[failure]
    )
    assert counts(database, case.tenant) == before


@pytest.mark.parametrize(
    "change,expected", [("session", 401), ("owner", 409), ("expiry", 409), ("metadata", 409)]
)
def test_head_is_outside_transactions_and_commit_revalidates_every_mutable_input(
    completion_case, database, change, expected
):
    case, active = completion_case, 0
    transaction = case.identity.transaction
    before = counts(database, case.tenant)

    @contextmanager
    def tracked():
        nonlocal active
        with transaction() as connection:
            active += 1
            try:
                yield connection
            finally:
                active -= 1

    def inspect(tenant_id, upload_id):
        assert active == 0
        assert counts(database, case.tenant) == before  # Preparation claim rolled back.
        if change == "session":
            update(
                database,
                "UPDATE sessions SET revoked_at=UTC_TIMESTAMP(3) WHERE tenant_id=:id",
                id=tenant_id,
            )
        elif change == "owner":
            update(
                database,
                "UPDATE inspection_items SET status='queued' WHERE id=:id",
                id=case.request["owner_id"],
            )
        elif change == "expiry":
            update(
                database,
                "UPDATE uploads SET expires_at=:expired WHERE id=:id",
                id=upload_id,
                expired=utc_now(),
            )
        else:
            update(
                database,
                "UPDATE uploads SET size_bytes=101,version=version+1 WHERE id=:id",
                id=upload_id,
            )
        return pinned(case)

    with (
        patch.object(case.identity, "transaction", side_effect=tracked),
        patch.object(case.storage, "inspect_staging", side_effect=inspect),
    ):
        response = complete(case)
    assert response.status_code == expected, response.text
    assert counts(database, case.tenant) == before


@pytest.mark.parametrize(
    "table", ["asset_images", "task_runs", "outbox_events", "audit_events", "api_idempotency"]
)
def test_any_commit_write_failure_rolls_back_image_task_dispatch_audit_and_response(
    completion_case, database, table
):
    case = completion_case
    before = counts(database, case.tenant)
    injected = []

    def fail(connection, cursor, statement, params, context, many):
        prefix = (
            "UPDATE api_idempotency SET state='completed'"
            if table == "api_idempotency"
            else f"INSERT INTO {table} "
        )
        if statement.startswith(prefix):
            injected.append(table)
            raise RuntimeError("private injected commit failure")

    event.listen(database, "before_cursor_execute", fail)
    try:
        with head(case):
            response = complete(case)
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert response.status_code == 500 and "private" not in response.text
    assert injected == [table]
    assert counts(database, case.tenant) == before
    assert rows(database, case, "uploads")[0]["status"] == "granted"


@pytest.mark.parametrize("same_key", [True, False])
def test_four_session_completion_race_pins_one_version_and_creates_one_validation_intent(
    completion_case, database, same_key
):
    case = completion_case
    clients = [TestClient(case.app, base_url=ORIGIN) for _ in range(4)]
    sessions = [login(client, case.tenant).json() for client in clients]
    key, barrier = str(uuid4()), Barrier(4)
    before = counts(database, case.tenant)

    def inspect(tenant, upload):
        index = barrier.wait(timeout=20)
        return ObjectVersion(case.grant["object_key"], f"concurrent-v{index}", 100, "image/png")

    def run(index):
        return post(
            clients[index],
            sessions[index],
            "/uploads/complete",
            case.body,
            key=key if same_key else str(uuid4()),
        )

    try:
        with (
            patch.object(case.storage, "inspect_staging", side_effect=inspect),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            responses = list(pool.map(run, range(4)))
        assert [r.status_code for r in responses] == [202] * 4, [r.text for r in responses]
        assert len({r.json()["data"]["id"] for r in responses}) == 1
        if same_key:
            assert len({r.content for r in responses}) == 1
        after = counts(database, case.tenant)
        assert {name: after[name] - before[name] for name in before} == {
            "uploads": 0,
            "asset_images": 1,
            "task_runs": 1,
            "outbox_events": 1,
            "api_idempotency": 1 if same_key else 4,
            "audit_events": 1,
        }
        assert rows(database, case, "asset_images")[0]["original_object_version"].startswith(
            "concurrent-v"
        )
    finally:
        for client in clients:
            client.close()


def test_upload_and_image_scope_are_checked_even_for_cached_completion(completion_case, database):
    case, key = completion_case, str(uuid4())
    with head(case):
        response = complete(case, key=key)
    image_id = response.json()["data"]["id"]
    foreign = create_tenant(database, "imageforeign")
    with TestClient(case.app, base_url=ORIGIN) as http:
        actor = login(http, foreign).json()
        assert post(http, actor, "/uploads/complete", case.body).status_code == 404
        assert http.get(f"/api/v1/images/{image_id}").status_code == 404
    # Revoke the issuing user's role without relying only on cached session state.
    update(
        database,
        "DELETE FROM user_roles WHERE tenant_id=:tenant AND user_id=:user",
        tenant=case.tenant["tenant_id"],
        user=case.tenant["admin_id"],
    )
    with patch.object(
        case.storage, "inspect_staging", side_effect=AssertionError("No unauthorized object access")
    ):
        assert complete(case, key=key).status_code == 404
        assert case.http.get(f"/api/v1/images/{image_id}").status_code == 404


def test_other_lab_image_and_completion_are_hidden_before_any_object_access(completion_case):
    case = completion_case
    with head(case):
        image = complete(case).json()["data"]
    _, other_lab, _, _ = inspection(case.http, case.session)
    user = create_user(case.http, case.session)
    assert (
        role_write(
            case.http, case.session, user["id"], command(role="inspector", lab=other_lab["id"])
        ).status_code
        == 200
    )
    with TestClient(case.app, base_url=ORIGIN) as http:
        actor = login(http, case.tenant, username="target").json()
        with patch.object(
            case.storage, "inspect_staging", side_effect=AssertionError("No cross-lab HEAD")
        ):
            assert post(http, actor, "/uploads/complete", case.body).status_code == 404
            assert http.get(f"/api/v1/images/{image['id']}").status_code == 404


@pytest.mark.parametrize("role", ["viewer", "lab_manager", "inspector", "remediator"])
def test_image_read_permission_is_not_capture_or_assignee_permission(
    completion_case, database, role
):
    case = completion_case
    with head(case):
        image = complete(case).json()["data"]
    user = create_user(case.http, case.session)
    assert (
        role_write(
            case.http, case.session, user["id"], command(role=role, lab=case.lab["id"])
        ).status_code
        == 200
    )
    with TestClient(case.app, base_url=ORIGIN) as http:
        actor = login(http, case.tenant, username="target").json()
        before = counts(database, case.tenant)
        assert http.get(f"/api/v1/images/{image['id']}").json()["data"] == image
        assert post(http, actor, "/uploads/complete", case.body).status_code == 403
        assert counts(database, case.tenant) == before


def test_image_reads_keep_bounded_fallback_but_completion_fails_closed(completion_case, database):
    case = completion_case
    with head(case):
        image = complete(case).json()["data"]
    before = counts(database, case.tenant)
    with patch.object(case.identity.limits.redis, "eval", side_effect=ConnectionError("private")):
        assert case.http.get(f"/api/v1/images/{image['id']}").status_code == 200
        assert complete(case).status_code == 503
    assert counts(database, case.tenant) == before

    def revoke(*args, **kwargs):
        update(
            database,
            "UPDATE sessions SET revoked_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant",
            tenant=case.tenant["tenant_id"],
        )

    with patch.object(case.identity.limits, "user", side_effect=revoke):
        assert case.http.get(f"/api/v1/images/{image['id']}").status_code == 401
    assert counts(database, case.tenant) == before


@pytest.mark.parametrize("status", ["validating", "ready", "rejected", "deleted"])
def test_image_status_projection_uses_persisted_metadata_not_synthetic_success(
    completion_case, database, status
):
    case = completion_case
    with head(case):
        image = complete(case).json()["data"]
    # Explicit synthetic later state to test read projection, not worker/deletion completion.
    update(
        database,
        "UPDATE asset_images SET status=:status WHERE id=:id",
        status=status,
        id=image["id"],
    )
    response = case.http.get(f"/api/v1/images/{image['id']}")
    assert response.status_code == 200 and response.json()["data"]["status"] == status
    assert_schema(response, "Image")
    if status == "deleted":
        with patch.object(
            case.storage, "inspect_staging", side_effect=AssertionError("No resurrection")
        ):
            assert complete(case).status_code == 404


def test_remediation_completion_requires_current_assignee_even_with_completed_parent(
    completion_case, database
):
    case = completion_case
    user = create_user(case.http, case.session)
    assert (
        role_write(
            case.http, case.session, user["id"], command(role="remediator", lab=case.lab["id"])
        ).status_code
        == 200
    )
    with database.begin() as connection:

        def add(table, **values):
            return insert(connection, table, tenant_id=case.tenant["tenant_id"], **values)

        # Legal FK synthetic history; remediation dispatch is not implemented in this batch.
        dictionary = add("dictionary_versions")
        model = add("model_versions", dictionary_version_id=dictionary)
        rules = add("rule_bundles")
        item_id = case.request["owner_id"]
        run = add(
            "inference_runs",
            item_id=item_id,
            laboratory_id=case.lab["id"],
            model_bundle_id=model,
            dictionary_version_id=dictionary,
            rule_bundle_id=rules,
        )
        fact = add("fact_revisions", item_id=item_id, run_id=run)
        evaluation = add(
            "rule_evaluations",
            item_id=item_id,
            run_id=run,
            fact_revision_id=fact,
            rule_bundle_id=rules,
        )
        finding = add(
            "findings",
            item_id=item_id,
            run_id=run,
            fact_revision_id=fact,
            rule_evaluation_id=evaluation,
            status="dispatched",
        )
        task = add(
            "remediation_tasks",
            finding_id=finding,
            laboratory_id=case.lab["id"],
            assignee_id=user["id"],
            status="in_progress",
        )
        connection.execute(
            text("UPDATE inspections SET status='completed' WHERE id=:id"),
            {"id": case.inspection["id"]},
        )
    with TestClient(case.app, base_url=ORIGIN) as http:
        actor = login(http, case.tenant, username="target").json()
        grant = post(
            http, actor, "/uploads", upload_body(task, owner_type="remediation_task")
        ).json()["data"]
        body = {"upload_id": grant["upload_id"], "sha256": "a" * 64}
        assert post(case.http, case.session, "/uploads/complete", body).status_code == 403
        with patch.object(
            case.storage,
            "inspect_staging",
            return_value=ObjectVersion(grant["object_key"], "task-v1", 100, "image/png"),
        ):
            result = post(http, actor, "/uploads/complete", body)
        assert result.status_code == 202, result.text
        assert result.json()["data"]["owner_id"] == task
        assert result.json()["data"]["owner_type"] == "remediation_task"
        assert count_rows(database, case.tenant, "asset_images") == 1
        update(
            database,
            "UPDATE remediation_tasks SET assignee_id=:user WHERE id=:id",
            user=case.tenant["admin_id"],
            id=task,
        )
        assert post(http, actor, "/uploads/complete", body).status_code == 403
