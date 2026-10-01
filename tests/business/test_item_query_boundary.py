"""Inspection item HTTP contracts without database dependencies."""

from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from apps.api.app.main import create_app
from packages.domain.security import ServiceError
from packages.persistence.security import SESSION_COOKIE
from tests.business.test_identity_boundary import ORIGIN
from tests.helpers import configured

ID = str(uuid4())
LIST = f"/api/v1/inspections/{ID}/items"
DETAIL = f"/api/v1/inspection-items/{ID}"


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.item_queries = service
    return TestClient(app, base_url=ORIGIN)


@pytest.mark.parametrize(
    "query",
    [
        "page=0",
        "page=-1",
        "page=1.5",
        "page=true",
        "page=",
        "page_size=0",
        "page_size=101",
        "page_size=-1",
        "page=1&page=2",
        "page_size=1&page_size=2",
        "laboratory_id=bad",
        "laboratory_id=",
        f"laboratory_id={ID}&laboratory_id={ID}",
        "tenant_id=x",
        "status=draft",
    ],
)
def test_list_rejects_invalid_ambiguous_or_unknown_queries(query):
    service = Mock()
    with client(service) as http:
        result = http.get(LIST + "?" + query)
    assert result.status_code == 422
    assert result.headers["cache-control"] == "no-store"
    service.read.assert_not_called()


@pytest.mark.parametrize("query", ["page=1", "page_size=20", f"laboratory_id={ID}", "x=1"])
def test_detail_rejects_all_query_parameters(query):
    service = Mock()
    with client(service) as http:
        assert http.get(DETAIL + "?" + query).status_code == 422
    service.read.assert_not_called()


def test_query_dispatch_defaults_boundaries_and_uuid_normalization():
    service = Mock()
    service.read.return_value = {"data": {}, "request_id": ID}
    with client(service) as http:
        http.cookies.set(SESSION_COOKIE, "token")
        assert http.get(LIST).status_code == 200
        assert service.read.call_args.kwargs == dict(
            resource_id=ID, page=1, page_size=20, laboratory_id=None
        )
        result = http.get(
            LIST.replace(ID, ID.upper()) + f"?page=2&page_size=100&laboratory_id={ID.upper()}"
        )
        assert result.status_code == 200
        assert service.read.call_args.args[0] == "list"
        assert service.read.call_args.args[1] == "token"
        assert service.read.call_args.kwargs == dict(
            resource_id=ID, page=2, page_size=100, laboratory_id=ID
        )
        assert http.get(DETAIL.replace(ID, ID.upper())).status_code == 200
        assert service.read.call_args.args[0] == "get"
        assert service.read.call_args.kwargs == {"resource_id": ID}
        assert result.headers["cache-control"] == "no-store"
        operations = {
            route.operation_id for route in http.app.routes if hasattr(route, "operation_id")
        }
        assert {"listInspectionItems", "getInspectionItem"} <= operations


@pytest.mark.parametrize("path", [LIST, DETAIL])
def test_missing_configuration_and_invalid_resource_ids(path):
    with client(None) as http:
        result = http.get(path)
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert result.headers["cache-control"] == "no-store"
        assert http.get(path.replace(ID, "bad")).status_code == 422


@pytest.mark.parametrize("path", [LIST, DETAIL])
@pytest.mark.parametrize(
    "error,status",
    [
        (ServiceError("NOT_FOUND", 404, "Not found"), 404),
        (RuntimeError("internal database detail"), 500),
        (SQLAlchemyError("internal database detail"), 503),
    ],
)
def test_service_errors_use_shared_sanitized_envelope(path, error, status):
    service = Mock()
    service.read.side_effect = error
    with client(service) as http:
        response = http.get(path)
    assert response.status_code == status
    assert "internal database detail" not in response.text
    assert response.json()["request_id"]
    assert response.headers["cache-control"] == "no-store"
