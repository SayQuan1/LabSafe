"""Report download contract and exact-version signing without object network access."""

from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from packages.domain.report_download import ReportDownload, validate_reference
from packages.storage.s3 import BUCKET, S3Storage
from tests.business.test_foundation_boundary import ORIGIN
from tests.business.test_s3_storage import settings
from tests.helpers import configured

TENANT, EXPORT, LAB = str(uuid4()), str(uuid4()), str(uuid4())
CHECKSUM = "a" * 64


def reference(**changes):
    value = ReportDownload(
        TENANT,
        EXPORT,
        (LAB,),
        CHECKSUM,
        f"tenant/{TENANT}/lab/{LAB}/reports/{EXPORT}/{CHECKSUM}.csv",
        "report-v1",
        123,
        "text/csv; charset=utf-8",
    )
    return value.__class__(
        *(changes.get(name, getattr(value, name)) for name in value.__dataclass_fields__)
    )


def test_report_reference_binds_scope_checksum_key_version_and_size():
    validate_reference(reference())
    for changes in (
        {"key": "staging/report"},
        {"checksum": "bad"},
        {"object_version": "null"},
        {"size_bytes": 0},
        {"mime_type": "application/pdf"},
    ):
        with pytest.raises(ValueError):
            validate_reference(reference(**changes))


def test_report_signer_uses_exact_version_and_60_second_ttl_without_network():
    storage = S3Storage(settings())
    instant = datetime(2026, 10, 7, 1, 2, 3)
    try:
        with (
            patch("botocore.auth.get_current_datetime", return_value=instant),
            patch(
                "botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("network")
            ),
        ):
            grant = storage.presign_report_get(reference(object_version="version/+?=&:v1"))
        parts, query = urlsplit(grant.url), parse_qs(urlsplit(grant.url).query)
        assert parts.path == f"/{BUCKET}/{reference().key}"
        assert query["versionId"] == ["version/+?=&:v1"]
        assert query["X-Amz-Expires"] == ["60"]
        assert grant.expires_at == instant + timedelta(seconds=60)
    finally:
        storage.close()


def test_report_download_route_rejects_query_and_returns_grant():
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN, storage=Mock())
    service = Mock()
    service.storage = Mock()
    service.download.return_value = {
        "data": {"url": "https://labsafe.test/signed", "expires_at": "time"},
        "request_id": str(uuid4()),
    }
    app.state.reports = service
    with TestClient(app, base_url=ORIGIN) as http:
        response = http.get(f"/api/v1/reports/exports/{EXPORT}/download")
        assert response.status_code == 200
        assert response.headers["referrer-policy"] == "no-referrer"
        assert http.get(f"/api/v1/reports/exports/{EXPORT}/download?tenant_id=x").status_code == 422
    service.download.assert_called_once()
