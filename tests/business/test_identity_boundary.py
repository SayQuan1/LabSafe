"""HTTP adapter checks; database behavior is tested against disposable MySQL."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from apps.api.app.main import create_app
from packages.domain.security import ServiceError
from tests.helpers import configured

ORIGIN = "https://labsafe.test"
LOGIN = {"tenant_code": "org", "username": "admin", "password": "long-test-password"}


def client(service=None):
    with configured():
        return TestClient(
            create_app(identity=service or Mock(), public_origin=ORIGIN), base_url=ORIGIN
        )


def test_identity_rejects_origin_content_type_extra_fields_and_redacts_inputs():
    service = Mock()
    with client(service) as http:
        assert http.post("/api/v1/auth/login", json=LOGIN).status_code == 403
        assert (
            http.post(
                "/api/v1/auth/login", json=LOGIN, headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/api/v1/auth/login", content="username=x", headers={"Origin": ORIGIN}
            ).status_code
            == 415
        )
        result = http.post(
            "/api/v1/auth/login",
            json={**LOGIN, "tenant_id": str(uuid4())},
            headers={"Origin": ORIGIN},
        )
        assert result.status_code == 422
        assert LOGIN["password"] not in result.text
        assert not service.login.called


def test_cookie_flags_and_forwarded_ip_not_trusted():
    service = Mock()
    service.login.return_value = ({"data": {}, "request_id": str(uuid4())}, "s" * 43)
    with client(service) as http:
        result = http.post(
            "/api/v1/auth/login",
            json=LOGIN,
            headers={"Origin": ORIGIN, "X-Forwarded-For": "9.9.9.9"},
        )
        assert result.status_code == 200
        cookie = result.headers["set-cookie"].lower()
        for flag in ("secure", "httponly", "samesite=lax", "path=/", "max-age=28800"):
            assert flag in cookie
        assert service.login.call_args.args[1] != "9.9.9.9"
        assert result.headers["cache-control"] == "no-store"


def test_logout_and_write_header_validation():
    service = Mock()
    service.write.return_value = (200, {"data": {"ok": True}, "request_id": str(uuid4())})
    with client(service) as http:
        assert (
            http.post("/api/v1/auth/logout", json={}, headers={"Origin": ORIGIN}).status_code == 422
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": "s" * 43, "Idempotency-Key": "request-123"}
        result = http.post("/api/v1/auth/logout", json={}, headers=headers)
        assert result.status_code == 200
        assert "Max-Age=0" in result.headers["set-cookie"]
        assert (
            http.post("/api/v1/auth/logout", json={"extra": True}, headers=headers).status_code
            == 422
        )
        bad = {"expected_version": True, "reason": "test"}
        assert (
            http.post(f"/api/v1/users/{uuid4()}/disable", json=bad, headers=headers).status_code
            == 422
        )


def test_queries_and_exceptions_are_redacted():
    service = Mock()
    service.read.side_effect = RuntimeError("private password and database URL")
    with client(service) as http:
        for query in ("laboratory_id=bad", "page=0", "page_size=101", "page=1&page=2"):
            assert http.get("/api/v1/users?" + query).status_code == 422
        response = http.get("/api/v1/me")
        assert response.status_code == 500
        assert "password" not in response.text


def test_unconfigured_identity_does_not_fake_success():
    with configured():
        app = create_app(public_origin=ORIGIN)
    with TestClient(app, base_url=ORIGIN) as http:
        assert http.get("/api/v1/me").status_code == 503


def test_readiness_checks_identity_dependencies_without_claiming_system_readiness():
    service = Mock()
    with client(service) as http:
        response = http.get("/ready")
        assert response.status_code == 200
        assert response.json()["is_simulated"] is True
        assert "identity, I-02D foundations, I-02E item queries" in response.json()["scope"]
        assert (
            "optional I-02F1/F2 upload signing and validation acceptance only"
            in response.json()["scope"]
        )
        assert "readiness not implemented" in response.json()["scope"]
        service.readiness.assert_called_once_with()


@pytest.mark.parametrize(
    "failure",
    [
        ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Rate limiter unavailable"),
        ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Database revision is not ready"),
        OperationalError("private SQL", {}, Exception(2003, "private URL")),
    ],
)
def test_readiness_dependency_failures_are_503_and_health_remains_alive(failure):
    service = Mock()
    service.readiness.side_effect = failure
    with client(service) as http:
        response = http.get("/ready")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert "private" not in response.text
        assert http.get("/health").status_code == 200
