"""Public completion/image routing and security; service calls are isolated here."""

from unittest.mock import Mock

import pytest

from tests.business.test_upload_boundary import HEADERS, ID, client


def complete_body():
    return {"upload_id": ID, "sha256": "a" * 64}


def test_completion_returns_canonical_original_202_and_normalized_id():
    service = Mock()
    service.complete.return_value = (202, {"data": {"status": "validating"}, "request_id": ID})
    with client(service) as http:
        response = http.post(
            "/api/v1/uploads/complete",
            json={**complete_body(), "upload_id": ID.upper()},
            headers=HEADERS,
        )
    assert response.status_code == 202 and response.headers["cache-control"] == "no-store"
    assert service.complete.call_args.args[3] == complete_body()
    assert response.json()["request_id"] == ID


@pytest.mark.parametrize(
    "values",
    [
        {"upload_id": "bad"},
        {"sha256": "A" * 64},
        {"sha256": 1},
        {"owner_id": ID},
        {"tenant_id": ID},
        {"object_key": "untrusted"},
        {"version_id": "untrusted"},
        {"expected_version": 1},
        {"ready": True},
    ],
)
def test_completion_rejects_non_contract_input(values):
    service = Mock()
    with client(service) as http:
        response = http.post(
            "/api/v1/uploads/complete", json={**complete_body(), **values}, headers=HEADERS
        )
        assert response.status_code == 422
    service.complete.assert_not_called()


def test_completion_keeps_origin_json_csrf_key_and_query_guards():
    service = Mock()
    with client(service) as http:
        path = "/api/v1/uploads/complete"
        assert http.post(path, json=complete_body()).status_code == 403
        assert http.post(path, content="text", headers=HEADERS).status_code == 415
        assert (
            http.post(path + "?version=latest", json=complete_body(), headers=HEADERS).status_code
            == 422
        )
        for missing in ("X-CSRF-Token", "Idempotency-Key"):
            headers = {k: v for k, v in HEADERS.items() if k.lower() != missing.lower()}
            assert http.post(path, json=complete_body(), headers=headers).status_code == 422
    service.complete.assert_not_called()


def test_get_image_is_read_only_and_needs_no_write_headers():
    service = Mock()
    service.get_image.return_value = {"data": {"status": "validating"}, "request_id": ID}
    with client(service) as http:
        response = http.get(f"/api/v1/images/{ID.upper()}")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert service.get_image.call_args.args[1] == ID
    service.complete.assert_not_called()


@pytest.mark.parametrize("suffix", ["bad", ID + "?x=1", ID + "?x=1&x=2"])
def test_image_rejects_bad_id_or_query(suffix):
    service = Mock()
    with client(service) as http:
        assert http.get("/api/v1/images/" + suffix).status_code == 422
    service.get_image.assert_not_called()


def test_completion_and_image_are_disabled_without_configured_upload_service():
    with client(None) as http:
        assert (
            http.post("/api/v1/uploads/complete", json=complete_body(), headers=HEADERS).status_code
            == 503
        )
        assert http.get(f"/api/v1/images/{ID}").status_code == 503
