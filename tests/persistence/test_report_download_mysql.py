"""Report download authorization and exact object evidence on disposable MySQL."""

from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from packages.persistence.security import utc_now
from packages.storage.s3 import DownloadGrant
from tests.persistence.test_identity_mysql import ORIGIN, headers, login
from tests.persistence.test_report_export_mysql import body
from tests.persistence.test_roles_mysql import lab_fixture


@pytest.fixture
def download_case(identity, database):
    tenant, service, app = identity
    lab = lab_fixture(database, tenant)
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        yield tenant, service, http, session, body([lab])


def post_export(http, session, request):
    return http.post("/api/v1/reports/exports", json=request, headers=headers(session))


def test_ready_report_download_signs_exact_registered_object(download_case, database):
    tenant, _, http, session, request = download_case
    created = post_export(http, session, request)
    assert created.status_code == 202, created.text
    export_id = created.json()["data"]["id"]
    with database.begin() as connection:
        row = (
            connection.execute(
                text("SELECT tenant_id,filters FROM report_exports WHERE id=:id"), {"id": export_id}
            )
            .mappings()
            .one()
        )
        labs = row["filters"]["laboratory_ids"] if isinstance(row["filters"], dict) else None
        if labs is None:
            import json

            labs = json.loads(row["filters"])["laboratory_ids"]
        checksum = "a" * 64
        key = f"tenant/{row['tenant_id']}/lab/{labs[0]}/reports/{export_id}/{checksum}.csv"
        connection.execute(
            text(
                "UPDATE report_exports SET status='ready',object_key=:key,checksum=:checksum,"
                "object_version='exact-v1',size_bytes=123,expires_at=:expires WHERE id=:id"
            ),
            {
                "id": export_id,
                "key": key,
                "checksum": checksum,
                "expires": utc_now() + timedelta(hours=1),
            },
        )
    service = http.app.state.reports
    service.storage = Mock()
    service.storage.presign_report_get.return_value = DownloadGrant(
        "https://labsafe.test/signed", utc_now() + timedelta(seconds=60)
    )
    response = http.get(f"/api/v1/reports/exports/{export_id}/download")
    assert response.status_code == 200, response.text
    reference = service.storage.presign_report_get.call_args.args[0]
    assert reference.key == key and reference.object_version == "exact-v1"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
