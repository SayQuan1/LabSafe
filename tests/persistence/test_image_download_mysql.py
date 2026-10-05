"""HTTP/MySQL/Redis grants and local SDK signing; no live object storage or real evidence."""

import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

from packages.application.image_download import ImageDownloadApplication
from packages.application.job_execution import ImageExecution
from packages.domain.image_validation import ValidatedImage, image_keys
from packages.persistence.image_validation import write_image_result
from packages.persistence.security import revoke_user_sessions, utc_now
from packages.storage.s3 import BUCKET, DownloadGrant
from tests.persistence import test_upload_completion_mysql as completion
from tests.persistence.test_foundations_mysql import count_rows
from tests.persistence.test_identity_mysql import ORIGIN, assert_schema, login
from tests.persistence.test_job_execution_mysql import row, update
from tests.persistence.test_roles_mysql import command, create_user, lab_fixture, role_write
from tests.persistence.test_security_mysql import create_tenant

upload_api = completion.upload_api
completion_case = completion.completion_case


@pytest.fixture
def download_case(completion_case, database):
    case = completion_case
    with completion.head(case):
        case.image = completion.complete(case).json()["data"]["id"]
    with database.connect() as connection:
        message = json.loads(
            connection.scalar(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE tenant_id=:tenant AND event_type='TaskDispatch'"
                ),
                {"tenant": case.tenant["tenant_id"]},
            )
        )
    execution = ImageExecution(database)
    lease = execution.claim(message)
    original, analysis = image_keys(lease.tenant_id, lease.input, "b" * 64)
    result = ValidatedImage(original, "O/+?=&:v1", analysis, "A/+?=&:v1", "b" * 64, 7, 5)
    execution.commit(
        lease,
        lambda connection, source: write_image_result(
            connection, lease.tenant_id, lease.task_id, source, result
        ),
    )
    case.app.state.image_downloads = ImageDownloadApplication(case.identity, case.storage)
    case.result = result
    yield case
    with database.begin() as connection:
        for table in ("task_attempts", "outbox_events", "task_runs"):
            connection.execute(
                text(f"DELETE FROM {table} WHERE tenant_id=:tenant"),
                {"tenant": case.tenant["tenant_id"]},
            )


def download(case, variant=None, http=None):
    query = "" if variant is None else f"?variant={variant}"
    return (http or case.http).get(f"/api/v1/images/{case.image}/download" + query)


def audits(database, case):
    with database.connect() as connection:
        return [
            dict(value)
            for value in connection.execute(
                text(
                    "SELECT * FROM audit_events WHERE tenant_id=:tenant "
                    "AND action='image.download_original'"
                ),
                {"tenant": case.tenant["tenant_id"]},
            ).mappings()
        ]


@pytest.mark.parametrize("variant", [None, "analysis", "original"])
def test_grant_public_schema_fixed_version_and_original_audit(download_case, database, variant):
    case = download_case
    old = row(database, "asset_images", case.image)
    counts = {
        table: count_rows(database, case.tenant, table)
        for table in ("uploads", "task_runs", "task_attempts", "outbox_events", "api_idempotency")
    }
    response = download(case, variant)
    assert response.status_code == 200, response.text
    assert_schema(response, "DownloadGrant")
    assert response.headers["cache-control"] == "no-store"
    parts, query = (
        urlsplit(response.json()["data"]["url"]),
        parse_qs(urlsplit(response.json()["data"]["url"]).query),
    )
    selected = variant or "analysis"
    assert parts.netloc == "labsafe.test" and parts.path == f"/{BUCKET}/{old[selected + '_key']}"
    assert query["versionId"] == [old[selected + "_object_version"]]
    assert query["X-Amz-Expires"] == ["60"]
    assert row(database, "asset_images", case.image) == old
    assert {table: count_rows(database, case.tenant, table) for table in counts} == counts
    recorded = audits(database, case)
    assert len(recorded) == int(selected == "original")
    if recorded:
        audit = recorded[0]
        assert audit["actor_id"] == case.tenant["admin_id"] and audit["resource_id"] == case.image
        assert audit["request_id"] == response.json()["request_id"]
        assert json.loads(audit["changes"]) == {"variant": "original"}
        assert "Signature" not in json.dumps(audit, default=str)


@pytest.mark.parametrize("role", ["viewer", "inspector", "lab_manager", "remediator"])
def test_reader_can_get_analysis_but_never_original(download_case, database, role):
    case = download_case
    user = create_user(case.http, case.session, "download_reader")
    assert (
        role_write(
            case.http, case.session, user["id"], command(role=role, lab=case.lab["id"])
        ).status_code
        == 200
    )
    with TestClient(case.app, base_url=ORIGIN) as http:
        login(http, case.tenant, username="download_reader")
        assert download(case, http=http).status_code == 200
        with patch.object(case.storage, "presign_image_get") as sign:
            assert download(case, "original", http).status_code == 403
            sign.assert_not_called()
    assert audits(database, case) == []


def test_foreign_tenant_and_lab_never_sign(download_case, database):
    case = download_case
    foreign = create_tenant(database, "download_foreign")
    lab = lab_fixture(database, case.tenant)
    user = create_user(case.http, case.session, "other_lab")
    assert (
        role_write(case.http, case.session, user["id"], command(role="viewer", lab=lab)).status_code
        == 200
    )
    with patch.object(case.storage, "presign_image_get") as sign:
        for tenant, username in ((foreign, "admin"), (case.tenant, "other_lab")):
            with TestClient(case.app, base_url=ORIGIN) as http:
                login(http, tenant, username=username)
                assert download(case, http=http).status_code == 404
                assert download(case, "original", http).status_code == 404
        sign.assert_not_called()


@pytest.mark.parametrize("state", ["validating", "rejected", "deleted"])
def test_non_ready_never_signs_either_variant(download_case, database, state):
    case = download_case
    update(
        database, "UPDATE asset_images SET status=:state WHERE id=:id", state=state, id=case.image
    )
    with patch.object(case.storage, "presign_image_get") as sign:
        for variant in ("analysis", "original"):
            response = download(case, variant)
            assert (
                response.status_code == 409 and response.json()["error"]["code"] == "STATE_CONFLICT"
            )
        sign.assert_not_called()
    assert audits(database, case) == []


@pytest.mark.parametrize(
    "field,value,variant",
    [
        ("analysis_key", "staging/foreign", "analysis"),
        ("analysis_sha256", None, "analysis"),
        ("analysis_object_version", "null", "analysis"),
        ("original_key", "tenant/foreign/lab/foreign/original/x", "original"),
        ("original_object_version", "bad\nversion", "original"),
    ],
)
def test_corrupt_ready_reference_never_signs(download_case, database, field, value, variant):
    case = download_case
    update(
        database, f"UPDATE asset_images SET {field}=:value WHERE id=:id", value=value, id=case.image
    )
    with patch.object(case.storage, "presign_image_get") as sign:
        assert download(case, variant).status_code == 409
        sign.assert_not_called()


@pytest.mark.parametrize("kind", ["session", "roles", "image"])
def test_preflight_is_revalidated_before_signing(download_case, database, kind):
    case = download_case

    def limit(actor):
        with database.begin() as connection:
            if kind == "session":
                revoke_user_sessions(connection, actor.tenant_id, actor.user_id)
            elif kind == "roles":
                connection.execute(
                    text("DELETE FROM user_roles WHERE tenant_id=:tenant AND user_id=:user"),
                    {"tenant": actor.tenant_id, "user": actor.user_id},
                )
            else:
                connection.execute(
                    text("UPDATE asset_images SET status='deleted' WHERE id=:id"),
                    {"id": case.image},
                )

    with (
        patch.object(case.identity.limits, "download_grant", side_effect=limit),
        patch.object(case.storage, "presign_image_get") as sign,
    ):
        assert download(case, "original").status_code == (
            401 if kind == "session" else 404 if kind == "roles" else 409
        )
        sign.assert_not_called()
    assert audits(database, case) == []


def test_image_and_authorization_locked_during_local_signing(download_case, database):
    case = download_case
    actual = case.storage.presign_image_get

    def sign(source):
        for table, key in (("asset_images", case.image), ("users", case.tenant["admin_id"])):
            with database.begin() as other:
                with pytest.raises(OperationalError) as caught:
                    other.execute(
                        text(f"SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT"), {"id": key}
                    )
                assert caught.value.orig.args[0] == 3572
        return actual(source)

    with patch.object(case.storage, "presign_image_get", side_effect=sign):
        assert download(case, "original").status_code == 200
    with database.begin() as other:
        other.execute(
            text("SELECT id FROM asset_images WHERE id=:id FOR UPDATE NOWAIT"), {"id": case.image}
        )
    assert len(audits(database, case)) == 1


def test_audit_failure_never_returns_original_grant(download_case, database):
    case = download_case
    before = row(database, "asset_images", case.image)

    def fail(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith("INSERT INTO audit_events"):
            raise RuntimeError("private storage credential")

    event.listen(database, "before_cursor_execute", fail)
    try:
        response = download(case, "original")
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert response.status_code == 500 and "url" not in response.json()
    assert "private storage credential" not in response.text and "X-Amz" not in response.text
    assert audits(database, case) == [] and row(database, "asset_images", case.image) == before


@pytest.mark.parametrize("seconds", [-1, 61])
def test_invalid_lifetime_never_returns_grant_or_original_audit(download_case, database, seconds):
    case = download_case
    grant = DownloadGrant("https://private-url", utc_now() + timedelta(seconds=seconds))
    with patch.object(case.storage, "presign_image_get", return_value=grant):
        response = download(case, "original")
    assert response.status_code == 503 and "private-url" not in response.text
    assert audits(database, case) == []


def test_repeated_original_reads_are_audited_and_later_revocation_blocks_issuance(
    download_case, database
):
    case = download_case
    for _ in range(2):
        assert download(case, "original").status_code == 200
    assert len(audits(database, case)) == 2
    update(
        database,
        "DELETE FROM user_roles WHERE tenant_id=:tenant AND user_id=:user",
        tenant=case.tenant["tenant_id"],
        user=case.tenant["admin_id"],
    )
    with patch.object(case.storage, "presign_image_get") as sign:
        assert download(case, "original").status_code == 404
        sign.assert_not_called()
    assert len(audits(database, case)) == 2


def test_redis_failure_prevents_download_capability_with_no_fallback(download_case, database):
    case = download_case
    with (
        patch.object(case.identity.limits.redis, "eval", side_effect=ConnectionError("private")),
        patch.object(case.storage, "presign_image_get") as sign,
    ):
        assert download(case).status_code == 503
        assert download(case, "original").status_code == 503
        sign.assert_not_called()
    assert audits(database, case) == []


def test_rate_limit_before_signing_and_outside_transaction(download_case, database):
    case = download_case
    began = False

    def begin(_connection):
        nonlocal began
        began = True

    actual = case.identity.limits.download_grant

    def limit(actor):
        nonlocal began
        # Preflight transaction has closed; begin events during Redis would expose a new DB scope.
        began = False
        actual(actor)
        assert not began

    event.listen(database, "begin", begin)
    try:
        with patch.object(case.identity.limits, "download_grant", side_effect=limit):
            assert download(case).status_code == 200
    finally:
        event.remove(database, "begin", begin)
    with (
        patch.object(case.identity.limits.redis, "eval", return_value=[121, 10]),
        patch.object(case.storage, "presign_image_get") as sign,
    ):
        response = download(case)
        assert response.status_code == 429 and response.headers["retry-after"] == "10"
        sign.assert_not_called()
