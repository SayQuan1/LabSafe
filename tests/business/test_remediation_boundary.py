"""Strict remediation HTTP contracts without database dependencies."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from tests.business.test_identity_boundary import ORIGIN
from tests.helpers import configured

ID = str(uuid4())
HEADERS = {"Origin": ORIGIN, "Idempotency-Key": "remediation-key", "X-CSRF-Token": "s" * 43}
DISPATCH = {
    "expected_version": 1,
    "assignee_id": ID,
    "due_at": "2030-01-01T00:00:00Z",
    "priority": "high",
    "description": "Contain the confirmed risk",
}


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.remediation = service
    return TestClient(app, base_url=ORIGIN)


def test_remediation_writes_keep_strict_body_and_security_boundary():
    service = Mock()
    service.write.return_value = (201, {"data": {}, "request_id": ID})
    with client(service) as http:
        response = http.post(
            f"/api/v1/findings/{ID}/remediation-tasks", json=DISPATCH, headers=HEADERS
        )
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        assert (
            http.post(
                f"/api/v1/findings/{ID}/remediation-tasks",
                json={**DISPATCH, "tenant_id": ID},
                headers=HEADERS,
            ).status_code
            == 422
        )
        assert (
            http.post(
                f"/api/v1/findings/{ID}/remediation-tasks",
                json={**DISPATCH, "due_at": "2030-01-01T00:00:00"},
                headers=HEADERS,
            ).status_code
            == 422
        )
    assert service.write.call_args.args[0] == "dispatch"
    assert service.write.call_args.args[1] is None
    assert service.write.call_args.args[5]["due_at"] == DISPATCH["due_at"]


@pytest.mark.parametrize(
    "query",
    ["overdue=maybe", "page=1&page=2", "unknown=1", "laboratory_id=bad"],
)
def test_remediation_list_rejects_ambiguous_or_unknown_queries(query):
    service = Mock()
    with client(service) as http:
        response = http.get(f"/api/v1/remediation-tasks?{query}")
    assert response.status_code == 422
    service.read.assert_not_called()


def test_remediation_routes_expose_contract_operation_ids():
    with client(Mock()) as http:
        operations = {
            route.operation_id for route in http.app.routes if hasattr(route, "operation_id")
        }
    assert {
        "dispatchRemediation",
        "listRemediations",
        "getRemediation",
        "acceptRemediation",
        "submitevidenceRemediation",
        "recheckRemediation",
        "rejectRemediation",
        "cannotremediateRemediation",
        "reassignRemediation",
        "listEvidence",
    } <= operations
