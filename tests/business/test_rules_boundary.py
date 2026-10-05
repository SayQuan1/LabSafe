"""HTTP adapter checks for the rule configuration lifecycle."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from tests.business.test_identity_boundary import ORIGIN
from tests.helpers import configured

RULE_SET_ID = str(uuid4())
RULE_VERSION_ID = str(uuid4())
HEADERS = {
    "Origin": ORIGIN,
    "Idempotency-Key": "rules-test-key",
    "X-CSRF-Token": "s" * 43,
}
RULE_BUNDLE = {
    "rule_set_id": RULE_SET_ID,
    "version_label": "synthetic-v1",
    "rules": [],
    "evaluator_version": "rules-dnf-v1",
}


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.rules = service
    return TestClient(app, base_url=ORIGIN)


@pytest.mark.parametrize(
    "method,path,body,operation",
    [
        ("post", "/rule-sets", {"name": "Rules", "description": "Synthetic"}, "create"),
        ("post", "/rule-versions", {"bundle": RULE_BUNDLE, "reason": "Import"}, "import"),
        (
            "post",
            f"/rule-versions/{RULE_VERSION_ID}/submit-approval",
            {"expected_version": 1, "reason": "Submit"},
            "submit",
        ),
        (
            "post",
            f"/rule-versions/{RULE_VERSION_ID}/approve",
            {"expected_version": 2, "reason": "Approve"},
            "approve",
        ),
        (
            "post",
            f"/rule-versions/{RULE_VERSION_ID}/publish",
            {"expected_version": 3, "reason": "Publish"},
            "publish",
        ),
        (
            "post",
            f"/rule-versions/{RULE_VERSION_ID}/retire",
            {"expected_version": 4, "reason": "Retire"},
            "retire",
        ),
        (
            "post",
            "/rule-bundles",
            {"rule_version_ids": [RULE_VERSION_ID]},
            "create_bundle",
        ),
    ],
)
def test_rule_write_contract(method, path, body, operation):
    service = Mock()
    service.write.return_value = (
        201 if operation in {"create", "import", "create_bundle"} else 200,
        {"data": {}, "request_id": str(uuid4())},
    )
    with client(service) as http:
        response = getattr(http, method)("/api/v1" + path, json=body, headers=HEADERS)
    assert response.status_code == service.write.return_value[0], response.text
    assert response.headers["cache-control"] == "no-store"
    assert service.write.call_args.args[0:2] == (
        "rule_set"
        if operation == "create"
        else "rule_version"
        if operation in {"import", "submit", "approve", "publish", "retire"}
        else "rule_bundle",
        "create" if operation == "create_bundle" else operation,
    )


@pytest.mark.parametrize(
    "path,kind,resource_id",
    [
        ("/rule-sets", "rule_set", None),
        ("/rule-versions", "rule_version", None),
        (f"/rule-versions/{RULE_VERSION_ID}", "rule_version", RULE_VERSION_ID),
        ("/rule-bundles", "rule_bundle", None),
    ],
)
def test_rule_read_contract_and_query_allowlist(path, kind, resource_id):
    service = Mock()
    service.read.return_value = {"data": {}, "request_id": str(uuid4())}
    with client(service) as http:
        response = http.get("/api/v1" + path)
        assert response.status_code == 200, response.text
        assert http.get("/api/v1" + path + "?unexpected=1").status_code == 422
        if resource_id is None:
            assert http.get("/api/v1" + path + "?page=2&page_size=3").status_code == 200
            assert http.get("/api/v1" + path + "?laboratory_id=bad").status_code == 422
    assert service.read.call_args.args[0] == kind
    assert service.read.call_args.kwargs["resource_id"] == resource_id


def test_rule_write_rejects_extra_fields_missing_headers_and_unexpected_query():
    service = Mock()
    with client(service) as http:
        body = {"name": "Rules", "description": "Synthetic"}
        assert (
            http.post(
                "/api/v1/rule-sets", json={**body, "tenant_id": str(uuid4())}, headers=HEADERS
            ).status_code
            == 422
        )
        assert http.post("/api/v1/rule-sets", json=body).status_code == 403
        assert (
            http.post("/api/v1/rule-sets?unexpected=1", json=body, headers=HEADERS).status_code
            == 422
        )
        assert http.post("/api/v1/rule-sets", content="x", headers=HEADERS).status_code == 415
    service.write.assert_not_called()
