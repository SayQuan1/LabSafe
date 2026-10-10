"""Independent botocore oracle; the SDK stays out of the AI environment."""

from datetime import datetime, timezone
from unittest.mock import patch

from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from apps.ai_inference.analysis import AnalysisSettings, signed_get


def test_sigv4_matches_sdk_with_exact_encoded_version():
    settings = AnalysisSettings(
        "https://store.test:9443",
        ("https://store.test:9443",),
        frozenset(),
        "synthetic",
        "test-only",
    )
    instant = datetime(2026, 10, 7, 1, 2, 3, tzinfo=timezone.utc)
    path, headers = signed_get(settings, "tenant/synthetic/analysis/a.png", "v/+=%", instant)
    request = AWSRequest(method="GET", url=settings.endpoint + path)
    with patch("botocore.auth.get_current_datetime", return_value=instant):
        S3SigV4Auth(
            Credentials(settings.access_key, settings.secret_key), "s3", "us-east-1"
        ).add_auth(request)
    assert request.headers["Authorization"] == headers["authorization"]
    assert request.headers["X-Amz-Date"] == headers["x-amz-date"]
    assert request.headers["X-Amz-Content-SHA256"] == headers["x-amz-content-sha256"]
