"""Real SDK signing and stubbed S3 responses; NOT live MinIO/IAM/TLS acceptance."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from packages.domain.security import ServiceError
from packages.domain.uploads import MAX_BYTES, staging_key
from packages.storage.s3 import BUCKET, ObjectVersion, S3Settings, S3Storage

TENANT, UPLOAD = str(uuid4()), str(uuid4())
KEY = staging_key(TENANT, UPLOAD)


def settings(**values):
    return S3Settings(
        **{
            "internal_endpoint": "http://127.0.0.1:9000",
            "public_endpoint": "https://labsafe.test",
            "public_origin": "https://labsafe.test",
            "access_key": "synthetic-access",
            "secret_key": "synthetic-secret-not-deployment",
            **values,
        }
    )


@pytest.fixture
def storage():
    result = S3Storage(settings())
    yield result
    result.close()


@pytest.mark.parametrize(
    "origin",
    [
        "http://labsafe.test",
        "https://labsafe.test/",
        "https://labsafe.test:8443",
        "https://user:secret@labsafe.test",
        "https://labsafe.test?x=1",
        "https://labsafe.test#x",
        "https://labsafe.test?",
        "https://labsafe.test#",
        "https://labsafe.test/path",
        "https://lab safe.test",
    ],
)
def test_invalid_public_origins_never_fall_back_to_internal(origin):
    with pytest.raises(ValueError):
        settings(public_endpoint=origin, public_origin=origin)


@pytest.mark.parametrize(
    "values",
    [
        {"public_origin": "https://other.test"},
        {"bucket": "public"},
        {"region": "us-west-2"},
        {"access_key": ""},
        {"secret_key": "has space"},
        {"internal_endpoint": "http://user:secret@minio:9000"},
        {"internal_endpoint": "file:///x"},
    ],
)
def test_configuration_rejects_wrong_scope_or_credentials(values):
    with pytest.raises(ValueError):
        settings(**values)
    assert "synthetic-secret" not in repr(settings())


def test_explicit_credentials_ignore_default_profiles_and_metadata(monkeypatch, tmp_path):
    config = tmp_path / "aws-config"
    config.write_text("not valid configuration", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(config))
    monkeypatch.setenv("AWS_PROFILE", "untrusted-profile")
    monkeypatch.setenv("AWS_EC2_METADATA_SERVICE_ENDPOINT", "http://127.0.0.1:1")
    with patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("network")):
        instance = S3Storage(settings())
        try:
            assert instance.presign_put(TENANT, UPLOAD, "image/png").url.startswith(
                "https://labsafe.test/"
            )
        finally:
            instance.close()


def test_sigv4_content_type_path_host_ttl_and_no_network(storage):
    instant = datetime(2026, 10, 1, 2, 3, 4)
    with (
        patch("botocore.auth.get_current_datetime", return_value=instant),
        patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("network")),
    ):
        grant = storage.presign_put(TENANT, UPLOAD, "image/png")
        other_mime = storage.presign_put(TENANT, UPLOAD, "image/jpeg")
        other_key = storage.presign_put(TENANT, str(uuid4()), "image/png")
    url, query = urlsplit(grant.url), parse_qs(urlsplit(grant.url).query)
    assert url.netloc == "labsafe.test" and url.path == f"/{BUCKET}/{KEY}"
    assert query["X-Amz-SignedHeaders"] == ["content-type;host"]
    assert query["X-Amz-Expires"] == ["600"]
    assert query["X-Amz-Credential"][0].endswith("/20261001/us-east-1/s3/aws4_request")
    assert grant.expires_at == instant + timedelta(seconds=600)
    assert "Signature" not in repr(grant)
    for alternative in (other_mime, other_key):
        assert (
            parse_qs(urlsplit(alternative.url).query)["X-Amz-Signature"] != query["X-Amz-Signature"]
        )


@pytest.mark.parametrize(
    "change,code",
    [
        ({"VersionId": "null"}, "DEPENDENCY_UNAVAILABLE"),
        ({"VersionId": ""}, "DEPENDENCY_UNAVAILABLE"),
        ({"VersionId": "x" * 201}, "DEPENDENCY_UNAVAILABLE"),
        ({"ContentLength": 0}, "IMAGE_INVALID"),
        ({"ContentLength": MAX_BYTES + 1}, "IMAGE_TOO_LARGE"),
        ({"ContentType": "image/svg+xml"}, "UNSUPPORTED_MEDIA_TYPE"),
        ({"DeleteMarker": True}, "DEPENDENCY_UNAVAILABLE"),
    ],
)
def test_head_requires_versioned_bounded_images(storage, change, code):
    with Stubber(storage.internal) as stub:
        stub.add_response(
            "head_object",
            {"VersionId": "v1", "ContentLength": 4, "ContentType": "image/png", **change},
            {"Bucket": BUCKET, "Key": KEY},
        )
        with pytest.raises(ServiceError) as caught:
            storage.inspect_staging(TENANT, UPLOAD)
        assert caught.value.code == code


def test_head_pins_version_but_does_not_trust_declared_sha(storage):
    with Stubber(storage.internal) as stub:
        stub.add_response(
            "head_object",
            {
                "VersionId": "v1",
                "ContentLength": 4,
                "ContentType": "image/png",
                "Metadata": {"sha256": "a" * 64},
                "ETag": '"not-a-sha256"',
            },
            {"Bucket": BUCKET, "Key": KEY},
        )
        assert storage.inspect_staging(TENANT, UPLOAD) == ObjectVersion(KEY, "v1", 4, "image/png")


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("NoSuchKey", "OBJECT_NOT_FOUND"),
        ("NoSuchVersion", "OBJECT_NOT_FOUND"),
        ("404", "OBJECT_NOT_FOUND"),
        ("AccessDenied", "DEPENDENCY_UNAVAILABLE"),
        ("SlowDown", "DEPENDENCY_UNAVAILABLE"),
    ],
)
def test_provider_errors_are_sanitized(storage, provider, expected):
    with Stubber(storage.internal) as stub:
        stub.add_client_error(
            "head_object", service_error_code=provider, service_message="secret internal endpoint"
        )
        with pytest.raises(ServiceError) as caught:
            storage.inspect_staging(TENANT, UPLOAD)
    assert caught.value.code == expected and "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "kind", ["valid", "hash", "version", "mime", "size", "truncated", "overflow"]
)
def test_stream_uses_exact_version_recomputes_hash_and_closes(storage, kind):
    payload = b"synthetic-image-bytes"  # Hash fixture, deliberately NOT a decodable image.
    expected = hashlib.sha256(payload).hexdigest()
    raw = BytesIO(
        payload[:-1] if kind == "truncated" else payload + b"x" if kind == "overflow" else payload
    )
    pinned = ObjectVersion(KEY, "fixed-version", len(payload), "image/png")
    result = {
        "Body": raw,
        "VersionId": "changed" if kind == "version" else "fixed-version",
        "ContentLength": len(payload) + (1 if kind == "size" else 0),
        "ContentType": "image/jpeg" if kind == "mime" else "image/png",
        "Metadata": {"sha256": expected},
    }
    with Stubber(storage.internal) as stub:
        stub.add_response(
            "get_object", result, {"Bucket": BUCKET, "Key": KEY, "VersionId": "fixed-version"}
        )
        if kind == "valid":
            assert storage.verify_staging_hash(TENANT, UPLOAD, pinned, expected) == expected
        else:
            with pytest.raises(ServiceError) as caught:
                storage.verify_staging_hash(
                    TENANT, UPLOAD, pinned, "0" * 64 if kind == "hash" else expected
                )
            assert caught.value.code == ("HASH_MISMATCH" if kind == "hash" else "IMAGE_INVALID")
    assert raw.closed


def test_missing_fixed_version_does_not_fall_back_to_latest(storage):
    with Stubber(storage.internal) as stub:
        stub.add_client_error(
            "get_object",
            "NoSuchVersion",
            expected_params={"Bucket": BUCKET, "Key": KEY, "VersionId": "v1"},
        )
        with pytest.raises(ServiceError) as caught:
            storage.verify_staging_hash(
                TENANT, UPLOAD, ObjectVersion(KEY, "v1", 4, "image/png"), "a" * 64
            )
        assert caught.value.code == "OBJECT_NOT_FOUND"
        stub.assert_no_pending_responses()


def test_truncated_sdk_stream_and_network_failure_are_not_success(storage):
    raw = BytesIO(b"x")
    with Stubber(storage.internal) as stub:
        stub.add_response(
            "get_object",
            {
                "Body": StreamingBody(raw, 4),
                "VersionId": "v1",
                "ContentLength": 4,
                "ContentType": "image/png",
            },
        )
        with pytest.raises(ServiceError) as caught:
            storage.verify_staging_hash(
                TENANT, UPLOAD, ObjectVersion(KEY, "v1", 4, "image/png"), "a" * 64
            )
        assert caught.value.status == 503
    assert raw.closed
    with patch.object(
        storage.internal, "head_object", side_effect=EndpointConnectionError(endpoint_url="secret")
    ):
        with pytest.raises(ServiceError) as caught:
            storage.inspect_staging(TENANT, UPLOAD)
        assert caught.value.status == 503 and "secret" not in str(caught.value)


def test_foreign_pinned_key_rejected_without_s3_call(storage):
    pinned = ObjectVersion(KEY, "v1", 4, "image/png")
    with patch.object(storage.internal, "get_object") as get:
        with pytest.raises(ValueError):
            storage.verify_staging_hash(TENANT, UPLOAD, replace(pinned, key="other/key"), "a" * 64)
    get.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("X-Amz-Signature", ""),
        ("X-Amz-Credential", "wrong"),
        ("X-Amz-Expires", "3600"),
        ("X-Amz-SignedHeaders", "host"),
    ],
)
def test_presigner_drift_is_rejected_instead_of_returning_an_unusable_grant(storage, field, value):
    original = storage.presign_put(TENANT, UPLOAD, "image/png")
    parts = urlsplit(original.url)
    query = parse_qs(parts.query)
    query[field] = [value]
    modified = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
    with patch.object(storage.signing, "generate_presigned_url", return_value=modified):
        with pytest.raises(ServiceError) as caught:
            storage.presign_put(TENANT, UPLOAD, "image/png")
    assert caught.value.status == 503


def test_credential_files_are_explicit_and_errors_do_not_echo_contents(monkeypatch, tmp_path):
    access, secret = tmp_path / "access", tmp_path / "secret"
    access.write_text("synthetic-access\n", encoding="ascii")
    secret.write_text("synthetic-secret\n", encoding="ascii")
    for key, value in {
        "S3_ENDPOINT": "http://127.0.0.1:9000",
        "S3_PUBLIC_ENDPOINT": "https://labsafe.test",
        "S3_ACCESS_KEY_FILE": str(access),
        "S3_SECRET_KEY_FILE": str(secret),
        "S3_BUCKET": BUCKET,
        "S3_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(key, value)
    assert S3Settings.from_environment("https://labsafe.test").secret_key == "synthetic-secret"
    secret.write_text("private invalid value", encoding="ascii")
    with pytest.raises(ValueError) as caught:
        S3Settings.from_environment("https://labsafe.test")
    assert "private" not in str(caught.value) and str(secret) not in str(caught.value)
