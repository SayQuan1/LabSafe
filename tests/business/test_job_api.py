"""Job routes and pure replay boundaries, without real DB or object server."""

from dataclasses import replace
from unittest.mock import Mock, patch

import pytest
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from packages.domain.job_replay import replay_body, require_replay
from packages.domain.security import Principal, ServiceError
from packages.storage.s3 import BUCKET, ObjectVersion, S3Storage
from tests.business.test_foundation_boundary import HEADERS, ID, ORIGIN
from tests.business.test_s3_storage import KEY, TENANT, UPLOAD, settings
from tests.helpers import configured


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.jobs = service
    return TestClient(app, base_url=ORIGIN)


def body():
    return {"expected_version": 1, "reason": "Retry after storage recovery"}


def test_job_routes_normalize_ids_and_preserve_202():
    service = Mock()
    service.read.return_value = {"data": {}, "request_id": ID}
    service.replay.return_value = (202, {"data": {"state": "ready"}, "request_id": ID})
    with client(service) as http:
        assert http.get(f"/api/v1/jobs/{ID.upper()}").status_code == 200
        assert service.read.call_args.kwargs["job_id"] == ID
        assert (
            http.get(
                f"/api/v1/dead-letters?page=2&page_size=3&laboratory_id={ID.upper()}"
            ).status_code
            == 200
        )
        assert service.read.call_args.kwargs == {"page": 2, "page_size": 3, "laboratory_id": ID}
        response = http.post(
            f"/api/v1/dead-letters/{ID.upper()}/replay", json=body(), headers=HEADERS
        )
        assert response.status_code == 202 and response.headers["cache-control"] == "no-store"
        assert service.replay.call_args.args[3:5] == (ID, body())


@pytest.mark.parametrize(
    "values",
    [
        {"expected_version": True},
        {"expected_version": "1"},
        {"expected_version": 0},
        {"expected_version": 2147483648},
        {"reason": " "},
        {"reason": ""},
        {"reason": "x" * 2001},
        {"tenant_id": ID},
        {"generation": 1},
        {"object_version": "latest"},
    ],
)
def test_replay_rejects_non_contract_body(values):
    service = Mock()
    with client(service) as http:
        assert (
            http.post(
                f"/api/v1/dead-letters/{ID}/replay", json={**body(), **values}, headers=HEADERS
            ).status_code
            == 422
        )
    service.replay.assert_not_called()


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/jobs/{ID}?state=failed",
        "/api/v1/dead-letters?state=failed",
        "/api/v1/dead-letters?page=0",
        "/api/v1/dead-letters?page_size=101",
        "/api/v1/dead-letters?page=1&page=2",
    ],
)
def test_job_queries_reject_extra_duplicate_or_invalid_parameters(path):
    service = Mock()
    with client(service) as http:
        assert http.get(path).status_code == 422
    service.read.assert_not_called()


def test_replay_keeps_security_headers_origin_json_and_no_query():
    service = Mock()
    path = f"/api/v1/dead-letters/{ID}/replay"
    with client(service) as http:
        assert http.post(path, json=body()).status_code == 403
        assert http.post(path, content="text", headers=HEADERS).status_code == 415
        assert http.post(path + "?version=latest", json=body(), headers=HEADERS).status_code == 422
        for missing in ("X-CSRF-Token", "Idempotency-Key"):
            headers = {k: v for k, v in HEADERS.items() if k.lower() != missing.lower()}
            assert http.post(path, json=body(), headers=headers).status_code == 422
        assert (
            http.post(
                path, json=body(), headers=[*HEADERS.items(), ("Idempotency-Key", "duplicate")]
            ).status_code
            == 422
        )
    service.replay.assert_not_called()


def test_disabled_job_service_fails_closed():
    with client(None) as http:
        assert http.get(f"/api/v1/jobs/{ID}").status_code == 503
        assert http.get("/api/v1/dead-letters").status_code == 503
        assert (
            http.post(f"/api/v1/dead-letters/{ID}/replay", json=body(), headers=HEADERS).status_code
            == 503
        )


ACTOR = Principal(ID, TENANT, "synthetic", (("safety_admin", "tenant", None),))
TASK = {
    "tenant_id": TENANT,
    "task_type": "validate_image",
    "version": 1,
    "state": "failed",
    "replay_generation": 0,
    "dispatch_sequence": 1,
}


@pytest.mark.parametrize(
    "state,allowed",
    [
        ("failed", True),
        ("dead_letter", True),
        ("succeeded", False),
        ("leased", False),
        ("ready", False),
        ("retry_wait", False),
    ],
)
def test_replay_only_technical_terminal_task(state, allowed):
    if allowed:
        require_replay(ACTOR, {**TASK, "state": state}, object(), 1)
    else:
        with pytest.raises(ServiceError) as caught:
            require_replay(ACTOR, {**TASK, "state": state}, object(), 1)
        assert caught.value.code == "STATE_CONFLICT"


@pytest.mark.parametrize(
    "kind", ["no_admin", "tenant", "unsupported", "missing", "source", "version", "counter"]
)
def test_replay_guard_rejects_unauthorized_stale_or_invalid_source(kind):
    actor, task, source, version = ACTOR, TASK, object(), 1
    if kind == "no_admin":
        actor = replace(ACTOR, roles=(("viewer", "laboratory", ID),))
    elif kind == "tenant":
        task = {**TASK, "tenant_id": ID}
    elif kind == "unsupported":
        task = {**TASK, "task_type": "inference_pipeline"}
    elif kind == "missing":
        task = None
    elif kind == "source":
        source = None
    elif kind == "version":
        version = 2
    else:
        task = {**TASK, "dispatch_sequence": 2147483647}
    with pytest.raises(ServiceError):
        require_replay(actor, task, source, version)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"expected_version": 1, "reason": " "},
        {"expected_version": True, "reason": "x"},
        {"expected_version": 1, "reason": "x", "tenant": ID},
    ],
)
def test_internal_replay_body_is_strict(value):
    with pytest.raises(ServiceError):
        replay_body(value)


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"VersionId": "new-v2"},
        {"ContentLength": 11},
        {"ContentType": "image/jpeg"},
        {"DeleteMarker": True},
    ],
)
def test_replay_sdk_head_uses_exact_version_and_checks_metadata(change):
    storage = S3Storage(settings())
    pinned = ObjectVersion(KEY, "v1", 10, "image/png")
    try:
        with Stubber(storage.internal) as stub:
            stub.add_response(
                "head_object",
                {"VersionId": "v1", "ContentLength": 10, "ContentType": "image/png", **change},
                {"Bucket": BUCKET, "Key": KEY, "VersionId": "v1"},
            )
            if change:
                with pytest.raises(ServiceError) as caught:
                    storage.inspect_pinned_staging(TENANT, UPLOAD, pinned)
                assert caught.value.code == "STATE_CONFLICT"
            else:
                assert storage.inspect_pinned_staging(TENANT, UPLOAD, pinned) == pinned
            stub.assert_no_pending_responses()
    finally:
        storage.close()


@pytest.mark.parametrize(
    "provider,code",
    [("NoSuchVersion", "OBJECT_NOT_FOUND"), ("AccessDenied", "DEPENDENCY_UNAVAILABLE")],
)
def test_replay_head_missing_version_or_storage_failure_no_fallback(provider, code):
    storage = S3Storage(settings())
    try:
        with Stubber(storage.internal) as stub:
            stub.add_client_error(
                "head_object",
                provider,
                expected_params={"Bucket": BUCKET, "Key": KEY, "VersionId": "v1"},
            )
            with pytest.raises(ServiceError) as caught:
                storage.inspect_pinned_staging(
                    TENANT, UPLOAD, ObjectVersion(KEY, "v1", 10, "image/png")
                )
            assert caught.value.code == code
    finally:
        storage.close()


def test_foreign_pinned_reference_never_accesses_storage():
    storage = S3Storage(settings())
    try:
        with patch.object(storage.internal, "head_object") as head:
            with pytest.raises(ValueError):
                storage.inspect_pinned_staging(
                    TENANT, UPLOAD, ObjectVersion("foreign/key", "v1", 10, "image/png")
                )
            head.assert_not_called()
    finally:
        storage.close()
