"""Real MySQL/Redis + local SDK signing, without a live MinIO server."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from sqlalchemy import text

from packages.application.uploads import UploadGrantApplication
from packages.persistence.security import utc_now
from packages.storage.s3 import S3Storage
from tests.business.test_s3_storage import settings
from tests.domain.test_uploads import upload_body
from tests.persistence.factories import insert
from tests.persistence.test_foundations_mysql import count_rows, created, inspection_body, post
from tests.persistence.test_identity_mysql import ORIGIN, assert_schema, login
from tests.persistence.test_item_queries_mysql import inspection
from tests.persistence.test_roles_mysql import command, create_user, role_write
from tests.persistence.test_security_mysql import create_tenant


@pytest.fixture
def upload_api(identity):
    tenant, identity_service, app = identity
    storage = S3Storage(settings())
    app.state.uploads = UploadGrantApplication(identity_service, storage)
    with patch(
        "botocore.httpsession.URLLib3Session.send",
        side_effect=AssertionError("Unexpected S3 network I/O"),
    ):
        yield tenant, identity_service, app, storage
    storage.close()


def counts(database, tenant):
    return {
        table: count_rows(database, tenant, table)
        for table in (
            "uploads",
            "asset_images",
            "task_runs",
            "outbox_events",
            "api_idempotency",
            "audit_events",
        )
    }


def test_grant_persists_exact_contract_and_replay_is_byte_stable_without_workflow_side_effects(
    upload_api, database
):
    tenant, _, app, storage = upload_api
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, lab, _, _ = inspection(http, session)
        body, key = upload_body(value["item_ids"][0]), str(uuid4())
        before = counts(database, tenant)
        result = post(http, session, "/uploads", body, key=key)
        assert result.status_code == 201, result.text
        assert_schema(result, "UploadGrant")
        grant = result.json()["data"]
        with patch.object(
            storage, "presign_put", side_effect=AssertionError("Replay must not re-sign")
        ):
            assert post(http, session, "/uploads", body, key=key).content == result.content
        assert (
            post(
                http, session, "/uploads", {**body, "filename": "different.png"}, key=key
            ).status_code
            == 409
        )
        assert grant["object_key"] == f"staging/{tenant['tenant_id']}/{grant['upload_id']}"
        assert "filename" not in grant and grant["max_bytes"] == 15728640
        assert parse_qs(urlsplit(grant["put_url"]).query)["X-Amz-SignedHeaders"] == [
            "content-type;host"
        ]
        after = counts(database, tenant)
        assert {k: after[k] - before[k] for k in before} == {
            "uploads": 1,
            "asset_images": 0,
            "task_runs": 0,
            "outbox_events": 0,
            "api_idempotency": 1,
            "audit_events": 1,
        }
        with database.begin() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM uploads WHERE id=:id"), {"id": grant["upload_id"]}
                )
                .mappings()
                .one()
            )
            assert row["laboratory_id"] == lab["id"] and row["requested_by"] == tenant["admin_id"]
            assert (
                row["inspection_item_id"] == body["owner_id"] and row["remediation_task_id"] is None
            )
            assert row["status"] == "granted" and row["version"] == 1
            assert (
                row["expected_sha256"] == body["sha256"] and row["size_bytes"] == body["size_bytes"]
            )
            assert row["captured_at"].isoformat() == "2026-10-01T02:00:00.123000"
            audit = connection.execute(
                text("SELECT changes FROM audit_events WHERE resource_id=:id"),
                {"id": grant["upload_id"]},
            ).scalar_one()
            assert "Signature" not in str(audit) and "synthetic" not in str(audit)
        item = http.get(f"/api/v1/inspection-items/{body['owner_id']}").json()["data"]
        assert item["status"] == "draft" and item["version"] == 1 and item["allowed_actions"] == []


@pytest.mark.parametrize(
    "role", ["creator_inspector", "other_inspector", "viewer", "remediator", "rule_expert", "none"]
)
def test_grant_requires_creator_capture_and_scope(upload_api, database, role):
    tenant, _, app, _ = upload_api
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as reader:
        session = login(admin, tenant).json()
        value, lab, room, template = inspection(admin, session)
        user = create_user(admin, session)
        actual_role = "inspector" if role.endswith("inspector") else role
        if role != "none":
            assert (
                role_write(
                    admin,
                    session,
                    user["id"],
                    command(role=actual_role, lab=None if role == "rule_expert" else lab["id"]),
                ).status_code
                == 200
            )
        actor = login(reader, tenant, username="target").json()
        if role == "creator_inspector":
            value = created(
                reader, actor, "/inspections", inspection_body(lab, template, room), "Inspection"
            )
        response = post(reader, actor, "/uploads", upload_body(value["item_ids"][0]))
        expected = (
            201 if role == "creator_inspector" else 404 if role in {"rule_expert", "none"} else 403
        )
        assert response.status_code == expected, response.text
        assert count_rows(database, tenant, "uploads") == int(expected == 201)


@pytest.mark.parametrize(
    "item_status,parent_status",
    [
        ("queued", "in_progress"),
        ("quality_checking", "in_progress"),
        ("processing", "in_progress"),
        ("completed", "in_progress"),
        ("draft", "completed"),
        ("draft", "cancelled"),
    ],
)
def test_closed_or_busy_owner_rejects_grants_atomically(
    upload_api, database, item_status, parent_status
):
    tenant, _, app, _ = upload_api
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session)
        with database.begin() as connection:
            connection.execute(
                text("UPDATE inspections SET status=:status WHERE id=:id"),
                {"status": parent_status, "id": value["id"]},
            )
            connection.execute(
                text("UPDATE inspection_items SET status=:status WHERE id=:id"),
                {"status": item_status, "id": value["item_ids"][0]},
            )
        before = counts(database, tenant)
        result = post(http, session, "/uploads", upload_body(value["item_ids"][0]))
        assert result.status_code == 409, result.text
        assert counts(database, tenant) == before


def test_foreign_tenant_and_foreign_lab_owner_are_hidden(upload_api, database):
    tenant, _, app, _ = upload_api
    foreign = create_tenant(database, "uploadforeign")
    with TestClient(app, base_url=ORIGIN) as http:
        actor = login(http, foreign).json()
        foreign_inspection, _, _, _ = inspection(http, actor)
        session = login(http, tenant).json()
        for owner_id in (foreign_inspection["item_ids"][0], str(uuid4())):
            assert post(http, session, "/uploads", upload_body(owner_id)).status_code == 404
        _, lab, _, _ = inspection(http, session)
        other, _, _, _ = inspection(http, session)
        user = create_user(http, session)
        assert (
            role_write(
                http, session, user["id"], command(role="inspector", lab=lab["id"])
            ).status_code
            == 200
        )
        actor = login(http, tenant, username="target").json()
        assert post(http, actor, "/uploads", upload_body(other["item_ids"][0])).status_code == 404
    assert count_rows(database, tenant, "uploads") == 0


@pytest.mark.parametrize("failure", ["signer", "audit", "idempotency"])
def test_grant_failures_roll_back_all_rows(upload_api, database, failure):
    tenant, _, app, storage = upload_api
    target = {
        "signer": patch.object(storage, "presign_put", side_effect=RuntimeError("private")),
        "audit": patch(
            "packages.persistence.uploads.append_audit", side_effect=RuntimeError("private")
        ),
        "idempotency": patch(
            "packages.application.uploads.complete_idempotency", side_effect=RuntimeError("private")
        ),
    }[failure]
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session)
        before = counts(database, tenant)
        with target:
            result = post(http, session, "/uploads", upload_body(value["item_ids"][0]))
        assert result.status_code == 500 and "private" not in result.text
        assert counts(database, tenant) == before


def test_redis_failure_never_issues_or_persists_grant(upload_api, database):
    tenant, service, app, storage = upload_api
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session)
        before = counts(database, tenant)
        with (
            patch.object(service.limits.redis, "eval", side_effect=ConnectionError("private")),
            patch.object(storage, "presign_put") as sign,
        ):
            response = post(http, session, "/uploads", upload_body(value["item_ids"][0]))
        assert response.status_code == 503
        sign.assert_not_called()
        assert counts(database, tenant) == before


def test_db_grant_quota_is_not_just_redis_rate_and_expiry_frees_capacity(upload_api, database):
    tenant, service, app, _ = upload_api
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session)
        body = upload_body(value["item_ids"][0])
        for _ in range(10):
            result = post(http, session, "/uploads", body)
            assert result.status_code == 201, result.text
        limited = post(http, session, "/uploads", body)
        assert limited.status_code == 429 and limited.headers["retry-after"]
        # Bypass the separately-tested minute counter to exercise DB quota state.
        with patch.object(service.limits, "upload_grant"):
            assert post(http, session, "/uploads", body).status_code == 429
            with database.begin() as connection:
                connection.execute(
                    text("UPDATE uploads SET expires_at=:expired WHERE id=:id"),
                    {
                        "expired": utc_now() - timedelta(seconds=1),
                        "id": result.json()["data"]["upload_id"],
                    },
                )
            assert post(http, session, "/uploads", body).status_code == 201
        assert count_rows(database, tenant, "uploads") == 11


@pytest.mark.parametrize("same_key", [True, False])
def test_four_sessions_share_one_user_quota_without_shared_to_exclusive_upgrade(
    upload_api, database, same_key
):
    tenant, service, app, _ = upload_api
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session)
        body = upload_body(value["item_ids"][0])
        for _ in range(9):
            assert post(http, session, "/uploads", body).status_code == 201
    clients = [TestClient(app, base_url=ORIGIN) for _ in range(4)]
    sessions = [login(client, tenant).json() for client in clients]
    key, barrier = str(uuid4()), Barrier(4)

    def create(index):
        barrier.wait(timeout=10)
        return post(
            clients[index], sessions[index], "/uploads", body, key=key if same_key else str(uuid4())
        )

    try:
        with (
            patch.object(service.limits, "upload_grant"),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            results = list(pool.map(create, range(4)))
        assert sorted(r.status_code for r in results) == (
            [201] * 4 if same_key else [201, 429, 429, 429]
        ), [r.text for r in results]
        if same_key:
            assert len({r.content for r in results}) == 1
        assert count_rows(database, tenant, "uploads") == 10
    finally:
        for client in clients:
            client.close()


def test_preflight_session_revocation_and_rate_limit_transaction_boundary(upload_api, database):
    tenant, service, app, storage = upload_api
    active, transaction = 0, service.transaction

    @contextmanager
    def tracked():
        nonlocal active
        with transaction() as connection:
            active += 1
            try:
                yield connection
            finally:
                active -= 1

    def revoke(actor):
        assert active == 0
        with database.begin() as connection:
            connection.execute(
                text("UPDATE sessions SET revoked_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant"),
                {"tenant": tenant["tenant_id"]},
            )

    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session)
        before = counts(database, tenant)
        with (
            patch.object(service, "transaction", side_effect=tracked),
            patch.object(service.limits, "upload_grant", side_effect=revoke),
            patch.object(storage, "presign_put") as sign,
        ):
            assert (
                post(http, session, "/uploads", upload_body(value["item_ids"][0])).status_code
                == 401
            )
        sign.assert_not_called()
        assert counts(database, tenant) == before


def test_revoked_role_blocks_replay_and_completed_owner_does_not_refresh_original_grant(
    upload_api, database
):
    tenant, _, app, _ = upload_api
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as http:
        session = login(admin, tenant).json()
        _, lab, room, template = inspection(admin, session)
        user = create_user(admin, session)
        assert (
            role_write(
                admin, session, user["id"], command(role="inspector", lab=lab["id"])
            ).status_code
            == 200
        )
        actor = login(http, tenant, username="target").json()
        value = created(
            http, actor, "/inspections", inspection_body(lab, template, room), "Inspection"
        )
        body, key = upload_body(value["item_ids"][0]), str(uuid4())
        result = post(http, actor, "/uploads", body, key=key)
        assert result.status_code == 201
        with database.begin() as connection:
            connection.execute(
                text("UPDATE inspection_items SET status='completed' WHERE id=:id"),
                {"id": body["owner_id"]},
            )
        assert post(http, actor, "/uploads", body, key=key).content == result.content
        assert post(http, actor, "/uploads", body).status_code == 409
        assert (
            role_write(
                admin, session, user["id"], command(2, "inspector", lab["id"]), revoke=True
            ).status_code
            == 200
        )
        assert post(http, actor, "/uploads", body, key=key).status_code == 401
        actor = login(http, tenant, username="target").json()
        assert post(http, actor, "/uploads", body, key=key).status_code == 404


def test_remediation_grant_belongs_to_assignee_even_after_inspection_completes(
    upload_api, database
):
    tenant, _, app, _ = upload_api
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as http:
        session = login(admin, tenant).json()
        value, lab, _, _ = inspection(admin, session)
        user = create_user(admin, session)
        assert (
            role_write(
                admin, session, user["id"], command(role="remediator", lab=lab["id"])
            ).status_code
            == 200
        )
        with database.begin() as connection:

            def add(table, **values):
                return insert(connection, table, tenant_id=tenant["tenant_id"], **values)

            # Synthetic legal history because the remediation command chain is not yet implemented.
            dictionary = add("dictionary_versions")
            model = add("model_versions", dictionary_version_id=dictionary)
            rules = add("rule_bundles")
            item_id = value["item_ids"][0]
            run = add(
                "inference_runs",
                item_id=item_id,
                laboratory_id=lab["id"],
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
                laboratory_id=lab["id"],
                assignee_id=user["id"],
                status="in_progress",
            )
            connection.execute(
                text("UPDATE inspections SET status='completed' WHERE id=:id"), {"id": value["id"]}
            )
        body = upload_body(task, owner_type="remediation_task")
        assert post(admin, session, "/uploads", body).status_code == 403
        actor = login(http, tenant, username="target").json()
        result = post(http, actor, "/uploads", body)
        assert result.status_code == 201, result.text
        with database.begin() as connection:
            row = connection.execute(
                text("SELECT inspection_item_id,remediation_task_id FROM uploads WHERE id=:id"),
                {"id": result.json()["data"]["upload_id"]},
            ).one()
            assert row == (None, task)
