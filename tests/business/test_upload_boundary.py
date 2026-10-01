"""Grant route strictness and capability configuration; no real object server."""

from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from packages.domain.uploads import MAX_BYTES, validate_upload_request
from tests.business.test_foundation_boundary import HEADERS, ID, ORIGIN
from tests.domain.test_uploads import upload_body
from tests.helpers import configured


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.uploads = service
    return TestClient(app, base_url=ORIGIN)


def test_create_upload_dispatches_only_the_contract_and_uses_original_response():
    service = Mock()
    service.create.return_value = (201, {"data": {}, "request_id": ID})
    with client(service) as http:
        response = http.post("/api/v1/uploads", json=upload_body(ID.upper()), headers=HEADERS)
        assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
        assert service.create.call_args.args[3] == upload_body(ID)
        assert http.post("/api/v1/uploads/complete", json={}, headers=HEADERS).status_code == 422
    assert service.create.call_count == 1


def test_capture_time_has_no_undocumented_fractional_precision_limit():
    body = {**upload_body(), "captured_at": "2026-10-01T01:02:03." + "1234567890" * 10 + "Z"}
    assert validate_upload_request(body).microsecond == 123000
    service = Mock()
    service.create.return_value = (201, {"data": {}, "request_id": ID})
    with client(service) as http:
        response = http.post("/api/v1/uploads", json=body, headers=HEADERS)
        assert response.status_code == 201
    assert service.create.call_args.args[3] == body


@pytest.mark.parametrize(
    "patch",
    [
        {"size_bytes": True},
        {"size_bytes": "1"},
        {"size_bytes": 0},
        {"size_bytes": MAX_BYTES + 1},
        {"owner_type": "anything"},
        {"owner_id": "bad"},
        {"filename": ""},
        {"mime_type": "image/gif"},
        {"sha256": "A" * 64},
        {"tenant_id": ID},
        {"object_key": "bad"},
        {"put_url": "https://evil.test"},
        {"captured_at": 123},
    ],
)
def test_upload_rejects_unknown_and_non_strict_fields(patch):
    service = Mock()
    with client(service) as http:
        response = http.post("/api/v1/uploads", json={**upload_body(), **patch}, headers=HEADERS)
        assert response.status_code == 422, response.text
    service.create.assert_not_called()


def test_upload_keeps_same_origin_csrf_idempotency_and_query_guards():
    service = Mock()
    with client(service) as http:
        assert http.post("/api/v1/uploads", json=upload_body()).status_code == 403
        assert http.post("/api/v1/uploads", content="x", headers=HEADERS).status_code == 415
        assert (
            http.post("/api/v1/uploads", json=upload_body(), headers={"Origin": ORIGIN}).status_code
            == 422
        )
        assert (
            http.post("/api/v1/uploads?x=1", json=upload_body(), headers=HEADERS).status_code == 422
        )
        duplicate = [*HEADERS.items(), ("X-CSRF-Token", "duplicate")]
        assert (
            http.post("/api/v1/uploads", json=upload_body(), headers=duplicate).status_code == 422
        )
    service.create.assert_not_called()
    with client(None) as http:
        assert http.post("/api/v1/uploads", json=upload_body(), headers=HEADERS).status_code == 503


def test_upload_feature_configuration_is_fail_closed(monkeypatch):
    with configured():
        monkeypatch.setenv("API_UPLOADS_ENABLED", "yes")
        with pytest.raises(ValueError, match="API_UPLOADS_ENABLED"):
            create_app()
        monkeypatch.setenv("API_UPLOADS_ENABLED", "1")
        monkeypatch.setenv("API_IDENTITY_ENABLED", "0")
        with pytest.raises(ValueError, match="require configured identity"):
            create_app()


def test_invalid_upload_flag_is_checked_before_allocating_identity(monkeypatch):
    with configured(), patch("apps.api.app.main.configured_identity") as configure:
        monkeypatch.setenv("API_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("API_UPLOADS_ENABLED", "bad")
        with pytest.raises(ValueError, match="API_UPLOADS_ENABLED"):
            create_app()
        configure.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
def test_owned_storage_and_identity_are_closed_on_shutdown_or_startup_failure(monkeypatch, failure):
    identity, storage = Mock(), Mock()
    with (
        configured(),
        patch("apps.api.app.main.configured_identity", return_value=(identity, ORIGIN)),
        patch(
            "packages.storage.s3.S3Settings.from_environment",
            side_effect=ValueError("bad config") if failure else None,
        ),
        patch("packages.storage.s3.S3Storage", return_value=storage),
    ):
        monkeypatch.setenv("API_IDENTITY_ENABLED", "1")
        monkeypatch.setenv("API_UPLOADS_ENABLED", "1")
        if failure:
            with pytest.raises(ValueError, match="bad config"):
                create_app()
            storage.close.assert_not_called()
        else:
            with TestClient(create_app()):
                pass
            storage.close.assert_called_once()
        identity.engine.dispose.assert_called_once()
        identity.limits.redis.close.assert_called_once()
