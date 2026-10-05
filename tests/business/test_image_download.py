"""Download contracts and actual SigV4 cryptography, without object network access."""

import hashlib
import hmac
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from packages.application.rate_limit import RateLimits
from packages.domain.image_download import download_reference
from packages.domain.security import Principal, ServiceError
from packages.storage.s3 import BUCKET, S3Storage
from tests.business.test_foundation_boundary import ID, ORIGIN
from tests.business.test_s3_storage import settings
from tests.helpers import configured

TENANT, LAB = str(uuid4()), str(uuid4())
ACTOR = Principal(ID, TENANT, "synthetic", (("safety_admin", "tenant", None),))


def image_row(**values):
    root = f"tenant/{TENANT}/lab/{LAB}"
    return {
        "id": ID,
        "tenant_id": TENANT,
        "laboratory_id": LAB,
        "status": "ready",
        "mime_type": "image/jpeg",
        "original_sha256": "a" * 64,
        "analysis_sha256": "b" * 64,
        "original_key": f"{root}/original/{ID}/{'a' * 64}.jpg",
        "analysis_key": f"{root}/analysis/{ID}/{'b' * 64}.png",
        "original_object_version": "O-v1",
        "analysis_object_version": "A-v1",
        **values,
    }


def reference(variant="analysis", **values):
    return replace(download_reference(ACTOR, image_row(), variant), **values)


def client(service):
    with configured():
        app = create_app(identity=Mock(), public_origin=ORIGIN)
    app.state.image_downloads = service
    return TestClient(app, base_url=ORIGIN)


def test_download_route_defaults_analysis_normalizes_uuid_and_returns_only_grant():
    service = Mock()
    service.download.return_value = {
        "data": {"url": "https://synthetic", "expires_at": "time"},
        "request_id": ID,
    }
    with client(service) as http:
        response = http.get(f"/api/v1/images/{ID.upper()}/download")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert service.download.call_args.args[1:3] == (ID, "analysis")
        assert http.get(f"/api/v1/images/{ID}/download?variant=original").status_code == 200
        assert service.download.call_args.args[2] == "original"


@pytest.mark.parametrize(
    "query",
    [
        "variant=latest",
        "variant=",
        "variant=analysis&variant=original",
        "object_key=staging/x",
        "versionId=latest",
        "tenant_id=x",
    ],
)
def test_download_rejects_non_contract_query_before_service(query):
    service = Mock()
    with client(service) as http:
        assert http.get(f"/api/v1/images/{ID}/download?{query}").status_code == 422
    service.download.assert_not_called()


def test_disabled_download_fails_closed():
    with client(None) as http:
        assert http.get(f"/api/v1/images/{ID}/download").status_code == 503


@pytest.mark.parametrize("state", ["validating", "rejected", "deleted"])
def test_no_download_from_non_ready_state(state):
    with pytest.raises(ServiceError) as caught:
        download_reference(ACTOR, image_row(status=state), "original")
    assert caught.value.code == "STATE_CONFLICT"


@pytest.mark.parametrize("role", ["viewer", "inspector", "lab_manager", "remediator"])
def test_analysis_read_is_not_original_permission(role):
    actor = replace(ACTOR, roles=((role, "laboratory", LAB),))
    assert download_reference(actor, image_row(), "analysis").mime_type == "image/png"
    with pytest.raises(ServiceError) as caught:
        download_reference(actor, image_row(), "original")
    assert caught.value.status == 403


@pytest.mark.parametrize("change", [{"tenant_id": LAB}, {"laboratory_id": TENANT}])
def test_foreign_tenant_or_lab_hidden_before_evidence_validation(change):
    actor = replace(ACTOR, roles=(("viewer", "laboratory", LAB),))
    with pytest.raises(ServiceError) as caught:
        download_reference(
            actor, image_row(status="ready", analysis_key=None, **change), "analysis"
        )
    assert caught.value.status == 404


@pytest.mark.parametrize(
    "values",
    [
        {"analysis_key": "staging/untrusted"},
        {"analysis_sha256": None},
        {"analysis_object_version": None},
        {"analysis_object_version": "null"},
        {"analysis_object_version": "bad\nversion"},
    ],
)
def test_incomplete_ready_metadata_never_creates_reference(values):
    with pytest.raises(ServiceError) as caught:
        download_reference(ACTOR, image_row(**values), "analysis")
    assert caught.value.code == "STATE_CONFLICT"


def signature_for(url, secret, *, method="GET", version=None):
    parts, query = urlsplit(url), parse_qs(urlsplit(url).query)
    query.pop("X-Amz-Signature")
    if version is not None:
        query["versionId"] = [version]
    canonical_query = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v[0], safe='-_.~')}" for k, v in sorted(query.items())
    )
    canonical = "\n".join(
        (method, parts.path, canonical_query, f"host:{parts.netloc}\n", "host", "UNSIGNED-PAYLOAD")
    )
    credential = query["X-Amz-Credential"][0].split("/")
    scope = "/".join(credential[1:])
    signing = ("AWS4" + secret).encode()
    for component in credential[1:]:
        signing = hmac.new(signing, component.encode(), hashlib.sha256).digest()
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            query["X-Amz-Date"][0],
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        )
    )
    return hmac.new(signing, string_to_sign.encode(), hashlib.sha256).hexdigest()


@pytest.mark.parametrize("variant", ["analysis", "original"])
def test_real_sigv4_binds_method_host_key_version_and_ttl_without_network(variant):
    storage = S3Storage(settings())
    source = reference(variant, object_version="version/+?=&:v1")
    instant = datetime(2026, 10, 2, 1, 2, 3)
    try:
        with (
            patch("botocore.auth.get_current_datetime", return_value=instant),
            patch(
                "botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("network")
            ),
        ):
            grant = storage.presign_image_get(source)
        parts, query = urlsplit(grant.url), parse_qs(urlsplit(grant.url).query)
        assert parts.scheme == "https" and parts.netloc == "labsafe.test"
        assert parts.path == f"/{BUCKET}/{source.key}"
        assert query["versionId"] == [source.object_version]
        assert query["X-Amz-Expires"] == ["60"] and query["X-Amz-SignedHeaders"] == ["host"]
        assert grant.expires_at == instant + timedelta(seconds=60)
        signature = query["X-Amz-Signature"][0]
        assert signature_for(grant.url, storage.settings.secret_key) == signature
        assert signature_for(grant.url, storage.settings.secret_key, method="PUT") != signature
        assert signature_for(grant.url, storage.settings.secret_key, version="latest") != signature
        assert "Signature" not in repr(grant)
    finally:
        storage.close()


@pytest.mark.parametrize(
    "values",
    [
        {"key": "staging/x"},
        {"object_version": ""},
        {"object_version": "null"},
        {"object_version": "x" * 201},
        {"object_version": "bad\x00"},
        {"mime_type": "image/jpeg"},
        {"tenant_id": "../x"},
        {"sha256": "INVALID"},
    ],
)
def test_signer_rejects_noncanonical_reference_without_sdk_call(values):
    storage = S3Storage(settings())
    try:
        with patch.object(storage.signing, "generate_presigned_url") as sign:
            with pytest.raises(ValueError):
                storage.presign_image_get(reference(**values))
            sign.assert_not_called()
    finally:
        storage.close()


@pytest.mark.parametrize("mutation", ["host", "version", "ttl", "extra", "duplicate", "signature"])
def test_signer_rejects_malformed_or_misdirected_generated_url(mutation):
    storage = S3Storage(settings())
    try:
        source = reference()
        url = urlsplit(storage.presign_image_get(source).url)
        query = parse_qs(url.query)
        if mutation == "host":
            url = url._replace(netloc="untrusted.test")
        elif mutation == "version":
            query["versionId"] = ["latest"]
        elif mutation == "ttl":
            query["X-Amz-Expires"] = ["600"]
        elif mutation == "extra":
            query["response-content-type"] = ["image/svg+xml"]
        elif mutation == "duplicate":
            query["versionId"].append("latest")
        else:
            query["X-Amz-Signature"] = ["private-error"]
        malformed = urlunsplit(url._replace(query=urlencode(query, doseq=True)))
        with patch.object(storage.signing, "generate_presigned_url", return_value=malformed):
            with pytest.raises(ServiceError) as caught:
                storage.presign_image_get(source)
            assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
            assert "private-error" not in str(caught.value)
    finally:
        storage.close()


def test_signer_failure_sanitized():
    storage = S3Storage(settings())
    try:
        with patch.object(
            storage.signing,
            "generate_presigned_url",
            side_effect=EndpointConnectionError(endpoint_url="private-secret"),
        ):
            with pytest.raises(ServiceError) as caught:
                storage.presign_image_get(reference())
            assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
            assert "private-secret" not in str(caught.value)
    finally:
        storage.close()


def test_download_rate_uses_shared_read_quota_without_fallback_or_write_charge():
    limits = RateLimits(Mock())
    with patch.object(limits, "_charge") as charge:
        limits.download_grant(ACTOR)
    charge.assert_called_once_with("user", f"{TENANT}:{ID}", 120, 60)
