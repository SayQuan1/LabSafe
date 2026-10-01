"""Strict request contracts without database dependencies."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from tests.business.test_identity_boundary import ORIGIN
from tests.helpers import configured

ID = str(uuid4())
HEADERS = {"Origin": ORIGIN, "Idempotency-Key": "foundation-test-key", "X-CSRF-Token": "s" * 43}
ITEM = {
    "id": ID,
    "code": "item",
    "title": "Title",
    "capture_hint": "Hint",
    "sort_order": 0,
    "required": True,
}
COMMANDS = [
    ("/colleges", "college", "create", {"name": "College", "code": "col"}, None, None),
    (
        "/laboratories",
        "laboratory",
        "create",
        {"college_id": ID, "name": "Lab", "code": "lab"},
        None,
        None,
    ),
    (
        f"/laboratories/{ID}/locations",
        "location",
        "create",
        {"parent_id": None, "type": "room", "label": "Room"},
        None,
        ID,
    ),
    ("/templates", "template", "create", {"name": "Template", "items": [ITEM]}, None, None),
    (
        f"/templates/{ID}/publish",
        "template",
        "publish",
        {"expected_version": 1, "reason": "Publish"},
        ID,
        None,
    ),
    (
        f"/templates/{ID}/clone",
        "template",
        "clone",
        {"expected_version": 2, "reason": "Clone"},
        ID,
        None,
    ),
    (
        "/inspections",
        "inspection",
        "create",
        {"laboratory_id": ID, "template_id": ID, "location_ids": [ID]},
        None,
        None,
    ),
]


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.foundations = service
    return TestClient(app, base_url=ORIGIN)


@pytest.mark.parametrize("path,kind,operation,body,resource_id,lab", COMMANDS)
def test_write_contract(path, kind, operation, body, resource_id, lab):
    service = Mock()
    status = 200 if operation == "publish" else 201
    service.write.return_value = (status, {"data": {}, "request_id": ID})
    with client(service) as http:
        result = http.post("/api/v1" + path, json=body, headers=HEADERS)
        assert result.status_code == status, result.text
        assert result.headers["cache-control"] == "no-store"
    args = service.write.call_args
    assert args.args[:2] == (kind, operation) and args.args[5] == body
    assert args.kwargs == {"resource_id": resource_id, "laboratory_id": lab}


@pytest.mark.parametrize("path,kind,operation,body,resource_id,lab", COMMANDS)
def test_writes_keep_shared_security_boundary(path, kind, operation, body, resource_id, lab):
    service = Mock()
    with client(service) as http:
        for invalid in ({**body, "tenant_id": ID}, {}):
            assert http.post("/api/v1" + path, json=invalid, headers=HEADERS).status_code == 422
        assert http.post("/api/v1" + path, json=body).status_code == 403
        assert http.post("/api/v1" + path, content="x", headers=HEADERS).status_code == 415
        assert http.post("/api/v1" + path, json=body, headers={"Origin": ORIGIN}).status_code == 422
        duplicate = [*HEADERS.items(), ("X-CSRF-Token", "duplicate")]
        assert http.post("/api/v1" + path, json=body, headers=duplicate).status_code == 422
        assert (
            http.post("/api/v1" + path + "?unexpected=1", json=body, headers=HEADERS).status_code
            == 422
        )
    service.write.assert_not_called()


@pytest.mark.parametrize(
    "patch",
    [
        {"required": 1},
        {"required": "true"},
        {"sort_order": True},
        {"sort_order": "1"},
        {"sort_order": -1},
        {"sort_order": 2147483648},
        {"id": "bad"},
        {"id": 7},
        {"capture_hint": ""},
        {"extra": 1},
    ],
)
def test_strict_template_item(patch):
    service = Mock()
    with client(service) as http:
        result = http.post(
            "/api/v1/templates",
            headers=HEADERS,
            json={"name": "Template", "items": [{**ITEM, **patch}]},
        )
        assert result.status_code == 422, result.text
    service.write.assert_not_called()


def test_nullable_parent_required_and_uuid_normalization():
    service = Mock()
    service.write.return_value = (201, {"data": {}, "request_id": ID})
    with client(service) as http:
        path = f"/api/v1/laboratories/{ID}/locations"
        assert (
            http.post(path, json={"type": "room", "label": "R"}, headers=HEADERS).status_code == 422
        )
        assert (
            http.post(
                path, json={"parent_id": ID.upper(), "type": "area", "label": "A"}, headers=HEADERS
            ).status_code
            == 201
        )
    assert service.write.call_args.args[5]["parent_id"] == ID


@pytest.mark.parametrize(
    "path",
    ["/colleges", "/laboratories", "/templates", "/inspections", f"/laboratories/{ID}/locations"],
)
def test_pagination_query_boundaries(path):
    service = Mock()
    service.read.return_value = {"data": {}, "request_id": ID}
    with client(service) as http:
        for query in ["page=0", "page_size=101", "page=1&page=2", "unknown=1"]:
            assert http.get("/api/v1" + path + "?" + query).status_code == 422
        assert http.get("/api/v1" + path + "?page=2&page_size=3").status_code == 200
    assert service.read.call_args.kwargs["page"] == 2
    assert service.read.call_args.kwargs["page_size"] == 3


def test_query_scopes_and_unconfigured_foundations():
    service = Mock()
    service.read.return_value = {"data": {}, "request_id": ID}
    with client(service) as http:
        for path in ("/colleges", "/templates", f"/laboratories/{ID}/locations"):
            assert http.get("/api/v1" + path + f"?laboratory_id={ID}").status_code == 422
        for path in ("/laboratories", "/inspections"):
            assert http.get("/api/v1" + path + f"?laboratory_id={ID}").status_code == 200
            assert service.read.call_args.kwargs["laboratory_id"] == ID
            assert http.get("/api/v1" + path + "?laboratory_id=bad").status_code == 422
    with client(None) as http:
        assert http.get("/api/v1/colleges").status_code == 503
        assert (
            http.post(
                "/api/v1/colleges", json={"name": "N", "code": "C"}, headers=HEADERS
            ).status_code
            == 503
        )


def test_maximal_unicode_template_passes_bounded_body_limit():
    service = Mock()
    service.write.return_value = (201, {"data": {}, "request_id": ID})
    body = {
        "name": "T",
        "items": [
            {
                **ITEM,
                "id": str(uuid4()),
                "code": str(i) + "😀" * 60,
                "title": "😀" * 200,
                "capture_hint": "😀" * 1000,
                "sort_order": i,
            }
            for i in range(100)
        ],
    }
    with client(service) as http:
        assert http.post("/api/v1/templates", json=body, headers=HEADERS).status_code == 201
        assert (
            http.post(
                "/api/v1/templates",
                content=b" " * (2097152 + 1),
                headers={**HEADERS, "Content-Type": "application/json"},
            ).status_code
            == 422
        )
    assert service.write.call_count == 1
