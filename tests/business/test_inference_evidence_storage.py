"""Negative SDK responses and fixed-version integrity; no live S3 required."""

import hashlib
import io
from uuid import uuid4

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from packages.domain.inference_evidence import derivative_key
from packages.domain.security import ServiceError
from packages.storage.s3 import BUCKET, S3Storage
from tests.business.test_s3_storage import settings


@pytest.mark.parametrize(
    "case", ["ok", "version", "mime", "size", "hash", "oversize", "encoding", "missing"]
)
def test_analysis_get_exact_version_stream_sha_and_closed(case):
    tenant, lab, image = (str(uuid4()) for _ in range(3))
    raw = b"synthetic SDK payload, decoding tested separately"
    sha = hashlib.sha256(raw).hexdigest()
    key = f"tenant/{tenant}/lab/{lab}/analysis/{image}/{sha}.png"
    reference = {
        "image_id": image,
        "sha256": sha,
        "mime_type": "image/png",
        "object_key": key,
        "object_version": "exact/v1+=",
    }
    body = StreamingBody(io.BytesIO(raw), len(raw))
    reply = {
        "Body": body,
        "ContentLength": len(raw),
        "ContentType": "image/png",
        "VersionId": "exact/v1+=",
    }
    if case == "version":
        reply["VersionId"] = "different"
    elif case == "mime":
        reply["ContentType"] = "image/jpeg"
    elif case == "size":
        reply["ContentLength"] += 1
    elif case == "hash":
        body = StreamingBody(io.BytesIO(b"x" * len(raw)), len(raw))
        reply["Body"] = body
    elif case == "oversize":
        reply["ContentLength"] = 128 * 1024 * 1024 + 1
    elif case == "encoding":
        reply["ContentEncoding"] = "gzip"
    storage = S3Storage(settings())
    try:
        with Stubber(storage.internal) as stub:
            params = {"Bucket": BUCKET, "Key": key, "VersionId": reference["object_version"]}
            if case == "missing":
                stub.add_client_error(
                    "get_object",
                    service_error_code="NoSuchVersion",
                    http_status_code=404,
                    expected_params=params,
                )
            else:
                stub.add_response("get_object", reply, params)
            if case == "ok":
                assert storage.read_analysis(tenant, lab, reference) == raw
            else:
                with pytest.raises(ServiceError):
                    storage.read_analysis(tenant, lab, reference)
            stub.assert_no_pending_responses()
        if case != "missing":
            assert body._raw_stream.closed
    finally:
        storage.close()


@pytest.mark.parametrize("version", [None, "null", "", "has space", "v" * 201])
def test_upload_without_valid_exact_version_never_returns_artifact(version):
    tenant, lab, run, crop = (str(uuid4()) for _ in range(4))
    raw = b"synthetic PNG upload"
    key = derivative_key(tenant, lab, run, crop, hashlib.sha256(raw).hexdigest())
    storage = S3Storage(settings())
    try:
        with Stubber(storage.internal) as stub:
            stub.add_client_error(
                "head_object",
                service_error_code="NoSuchKey",
                http_status_code=404,
                expected_params={"Bucket": BUCKET, "Key": key},
            )
            stub.add_response(
                "put_object",
                {} if version is None else {"VersionId": version},
                {"Bucket": BUCKET, "Key": key, "Body": raw, "ContentType": "image/png"},
            )
            with pytest.raises(ServiceError):
                storage.put_derivative(key, raw)
            stub.assert_no_pending_responses()
    finally:
        storage.close()


def test_conflicting_existing_crop_is_not_overwritten():
    tenant, lab, run, crop = (str(uuid4()) for _ in range(4))
    raw = b"claimed crop"
    key = derivative_key(tenant, lab, run, crop, hashlib.sha256(raw).hexdigest())
    storage = S3Storage(settings())
    try:
        with Stubber(storage.internal) as stub:
            metadata = {
                "ContentLength": len(raw),
                "ContentType": "image/png",
                "VersionId": "old-v1",
            }
            stub.add_response("head_object", metadata, {"Bucket": BUCKET, "Key": key})
            body = StreamingBody(io.BytesIO(b"x" * len(raw)), len(raw))
            stub.add_response(
                "get_object",
                {**metadata, "Body": body},
                {"Bucket": BUCKET, "Key": key, "VersionId": "old-v1"},
            )
            with pytest.raises(ServiceError):
                storage.put_derivative(key, raw)
            stub.assert_no_pending_responses()
            assert body._raw_stream.closed
    finally:
        storage.close()
