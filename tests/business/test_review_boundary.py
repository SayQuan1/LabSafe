"""HTTP contract tests for facts and finding review endpoints."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from packages.persistence.review import _public_facts
from tests.business.test_foundation_boundary import HEADERS, ID
from tests.business.test_identity_boundary import ORIGIN
from tests.helpers import configured


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.review = service
    return TestClient(app, base_url=ORIGIN)


FACTS = {
    "expected_version": 1,
    "entities": [],
    "relations": [],
    "dates": [],
    "reason": "Corrected from the inspection label",
}
DECISION = {"expected_version": 1, "reason": "Reviewed against the image"}


@pytest.mark.parametrize(
    "path,body,operation",
    [
        (f"/inspection-items/{ID}/facts", FACTS, "facts"),
        (f"/findings/{ID}/confirm", DECISION, "confirm"),
        (f"/findings/{ID}/reject", DECISION, "reject"),
        (
            f"/findings/{ID}/cannot-determine",
            {**DECISION, "reason_code": "blur"},
            "cannot-determine",
        ),
    ],
)
def test_review_write_routes_preserve_status_and_payload(path, body, operation):
    service = Mock()
    service.write.return_value = (
        202 if operation == "facts" else 200,
        {"data": {}, "request_id": str(uuid4())},
    )
    with client(service) as http:
        response = http.post("/api/v1" + path, json=body, headers=HEADERS)
    assert response.status_code == service.write.return_value[0], response.text
    assert response.headers["cache-control"] == "no-store"
    assert service.write.call_args.args[0] == operation
    assert service.write.call_args.args[4] == ID
    assert service.write.call_args.args[5] == body


@pytest.mark.parametrize(
    "path,kind,resource_id",
    [
        (f"/inspection-items/{ID}/facts", "facts", ID),
        (f"/findings/{ID}", "finding", ID),
        ("/findings", "findings", None),
    ],
)
def test_review_read_routes_and_query_allowlist(path, kind, resource_id):
    service = Mock()
    service.read.return_value = {"data": {}, "request_id": str(uuid4())}
    with client(service) as http:
        assert http.get("/api/v1" + path).status_code == 200
        assert http.get("/api/v1" + path + "?unexpected=1").status_code == 422
        if kind == "findings":
            assert (
                http.get(
                    "/api/v1/findings?page=2&page_size=3&current_only=false"
                    "&status=needs_review&severity=critical&laboratory_id=" + ID
                ).status_code
                == 200
            )
            assert http.get("/api/v1/findings?current_only=1").status_code == 422
            assert http.get("/api/v1/findings?status=bogus").status_code == 422
        else:
            assert http.get("/api/v1" + path + "?page=2").status_code == 422
    assert service.read.call_args.args[0] == kind
    assert service.read.call_args.kwargs["resource_id"] == resource_id


@pytest.mark.parametrize(
    "body",
    [
        {**FACTS, "tenant_id": ID},
        {**FACTS, "expected_version": True},
        {**FACTS, "entities": [{"detection_id": ID}]},
        {**FACTS, "relations": [{"source_detection_id": ID, "target_detection_id": ID}]},
        {**FACTS, "dates": [{"detection_id": ID, "kind": "expiry", "value": "bad"}]},
    ],
)
def test_facts_edit_rejects_non_contract_body(body):
    service = Mock()
    with client(service) as http:
        response = http.post(f"/api/v1/inspection-items/{ID}/facts", json=body, headers=HEADERS)
    assert response.status_code == 422, response.text
    service.write.assert_not_called()


def test_review_routes_require_write_headers_and_fail_closed_without_service():
    with client(None) as http:
        assert http.get(f"/api/v1/findings/{ID}").status_code == 503
        assert http.post(f"/api/v1/findings/{ID}/reject", json=DECISION).status_code == 403
        assert (
            http.post(f"/api/v1/findings/{ID}/reject", content="x", headers=HEADERS).status_code
            == 415
        )


def test_model_facts_are_projected_to_public_fact_revision_shape():
    image_id, detection_id, entity_id = (str(uuid4()) for _ in range(3))
    entity = {
        "image_id": image_id,
        "detection_id": detection_id,
        "resolution": "resolved",
        "candidates": [{"entity_id": entity_id}],
    }
    projected = _public_facts("entities", [entity])
    assert projected == [
        {
            "detection_id": detection_id,
            "entity_id": entity_id,
            "resolution": "resolved",
            "source": "model",
            "evidence": [{"image_id": image_id, "detection_id": detection_id, "crop_id": None}],
        }
    ]
