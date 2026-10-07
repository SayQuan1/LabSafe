"""Strict report/export HTTP contracts without database dependencies."""

from unittest.mock import Mock
from uuid import uuid4

from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from tests.business.test_identity_boundary import ORIGIN
from tests.helpers import configured

ID = str(uuid4())
HEADERS = {"Origin": ORIGIN, "Idempotency-Key": "report-key", "X-CSRF-Token": "s" * 43}
BODY = {
    "format": "csv",
    "filters": {
        "laboratory_ids": [ID],
        "from": "2030-01-01T00:00:00Z",
        "to": "2030-01-31T00:00:00Z",
        "severity": ["high"],
        "finding_status": ["confirmed"],
    },
}


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.reports = service
    return TestClient(app, base_url=ORIGIN)


def test_create_export_is_strict_and_async():
    service = Mock()
    service.create.return_value = (202, {"data": {}, "request_id": ID})
    with client(service) as http:
        response = http.post("/api/v1/reports/exports", json=BODY, headers=HEADERS)
        assert response.status_code == 202
        assert (
            http.post(
                "/api/v1/reports/exports", json={**BODY, "tenant_id": ID}, headers=HEADERS
            ).status_code
            == 422
        )
        assert (
            http.post(
                "/api/v1/reports/exports",
                json={**BODY, "filters": {**BODY["filters"], "to": "2030-01-31T00:00:00"}},
                headers=HEADERS,
            ).status_code
            == 422
        )
    assert service.create.call_args.args[3]["filters"]["laboratory_ids"] == [ID]


def test_report_queries_reject_query_parameters_and_expose_operations():
    service = Mock()
    with client(service) as http:
        assert http.get(f"/api/v1/reports/exports/{ID}?page=1").status_code == 422
        operations = {
            route.operation_id for route in http.app.routes if hasattr(route, "operation_id")
        }
    assert {"createExport", "getExport", "replayJob"} <= operations
