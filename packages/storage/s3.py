"""Bounded S3 adapter. Presigning is local; HEAD/GET must run outside DB transactions."""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import boto3
import botocore.session
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from packages.domain.image_download import DOWNLOAD_SECONDS, validate_reference
from packages.domain.report_download import (
    REPORT_DOWNLOAD_SECONDS,
    ReportDownload,
)
from packages.domain.report_download import (
    validate_reference as validate_report_reference,
)
from packages.domain.report_execution import (
    MAX_REPORT_BYTES,
    REPORT_MIME,
    artifact_for,
    report_key,
)
from packages.domain.security import ServiceError
from packages.domain.uploads import (
    GRANT_SECONDS,
    MAX_BYTES,
    MIME_TYPES,
    digest,
    identifier,
    staging_key,
)

BUCKET = "labsafe-private"
MAX_ANALYSIS_BYTES = (
    128 * 1024 * 1024
)  # RGB PNG of at most 40M pixels, including encoding overhead.


def dependency():
    return ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Object storage unavailable")


def endpoint(value, *, public=False):
    try:
        parsed = urlsplit(value)
        if (
            not isinstance(value, str)
            or any(c.isspace() for c in value)
            or parsed.scheme not in ({"https"} if public else {"http", "https"})
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or "\\" in value
            or (public and parsed.port not in (None, 443))
        ):
            raise ValueError
        parsed.port  # Reject invalid port strings on internal endpoints too.
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid S3 endpoint configuration") from None
    return value


@dataclass(frozen=True, kw_only=True)
class S3Settings:
    internal_endpoint: str
    public_endpoint: str
    public_origin: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    bucket: str = BUCKET
    region: str = "us-east-1"

    def __post_init__(self):
        endpoint(self.internal_endpoint)
        endpoint(self.public_endpoint, public=True)
        if (
            self.public_endpoint != self.public_origin
            or self.bucket != BUCKET
            or self.region != "us-east-1"
        ):
            raise ValueError("S3 origin, bucket or region differs from the deployment contract")
        for secret in (self.access_key, self.secret_key):
            if (
                not isinstance(secret, str)
                or not 1 <= len(secret) <= 256
                or any(not 33 <= ord(c) <= 126 for c in secret)
            ):
                raise ValueError("Invalid S3 credentials")

    @classmethod
    def from_environment(cls, public_origin, *, worker=False):
        try:

            def secret(name):
                # No provider chain, ~/.aws, environment credential fallback or metadata service.
                return Path(os.environ[name]).read_text(encoding="ascii").rstrip("\r\n")

            return cls(
                internal_endpoint=os.environ["S3_ENDPOINT"],
                public_endpoint=os.environ["S3_PUBLIC_ENDPOINT"],
                public_origin=public_origin,
                access_key=secret("S3_WORKER_ACCESS_KEY_FILE" if worker else "S3_ACCESS_KEY_FILE"),
                secret_key=secret("S3_WORKER_SECRET_KEY_FILE" if worker else "S3_SECRET_KEY_FILE"),
                bucket=os.environ.get("S3_BUCKET", BUCKET),
                region=os.environ.get("S3_REGION", "us-east-1"),
            )
        except (OSError, UnicodeError, KeyError, ValueError):
            raise ValueError("S3 configuration or credential files are invalid") from None


@dataclass(frozen=True)
class PutGrant:
    url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class DownloadGrant:
    url: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class ObjectVersion:
    key: str
    version_id: str
    size_bytes: int
    mime_type: str


class S3Storage:
    def __init__(self, settings: S3Settings):
        self.settings = settings
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=2,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 2},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            proxies={},
        )
        isolated = botocore.session.Session()
        isolated.set_config_variable("config_file", os.devnull)
        isolated.set_config_variable("credentials_file", os.devnull)
        isolated.get_component("config_store").set_config_variable("profile", None)
        session = boto3.session.Session(
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            region_name=settings.region,
            botocore_session=isolated,
        )
        self.internal = session.client("s3", endpoint_url=settings.internal_endpoint, config=config)
        try:
            self.signing = session.client(
                "s3", endpoint_url=settings.public_endpoint, config=config
            )
        except Exception:
            self.internal.close()
            raise

    def close(self):
        try:
            self.internal.close()
        finally:
            self.signing.close()

    def presign_put(self, tenant_id, upload_id, mime_type):
        """Local SigV4 only: fixed credentials, no network or credential refresh."""
        if mime_type not in MIME_TYPES:
            raise ValueError("Unsupported grant content type")
        key = staging_key(tenant_id, upload_id)
        try:
            url = self.signing.generate_presigned_url(
                "put_object",
                Params={"Bucket": BUCKET, "Key": key, "ContentType": mime_type},
                ExpiresIn=GRANT_SECONDS,
                HttpMethod="PUT",
            )
            parts, origin = urlsplit(url), urlsplit(self.settings.public_origin)
            query = parse_qs(parts.query, strict_parsing=True)
            if (
                parts.scheme != origin.scheme
                or parts.netloc != origin.netloc
                or parts.path != f"/{BUCKET}/{key}"
                or parts.fragment
                or len(url) > 4096
                or any(len(v) != 1 for v in query.values())
                or query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]
                or query.get("X-Amz-Expires") != [str(GRANT_SECONDS)]
                or query.get("X-Amz-SignedHeaders") != ["content-type;host"]
            ):
                raise ValueError
            started = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ")
            expected_credential = (
                f"{self.settings.access_key}/{started:%Y%m%d}/us-east-1/s3/aws4_request"
            )
            if (
                query.get("X-Amz-Credential") != [expected_credential]
                or re.fullmatch(r"[a-f0-9]{64}", query.get("X-Amz-Signature", [""])[0]) is None
            ):
                raise ValueError
            return PutGrant(url, started + timedelta(seconds=GRANT_SECONDS))
        except (BotoCoreError, ClientError, ValueError, KeyError, TypeError):
            raise dependency() from None

    def presign_image_get(self, reference):
        """Local SigV4 for one canonical business image and its exact stored VersionId."""
        validate_reference(reference)
        try:
            url = self.signing.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": BUCKET,
                    "Key": reference.key,
                    "VersionId": reference.object_version,
                },
                ExpiresIn=DOWNLOAD_SECONDS,
                HttpMethod="GET",
            )
            parts, origin = urlsplit(url), urlsplit(self.settings.public_origin)
            query = parse_qs(parts.query, strict_parsing=True)
            if (
                parts.scheme != origin.scheme
                or parts.netloc != origin.netloc
                or parts.path != f"/{BUCKET}/{reference.key}"
                or parts.fragment
                or len(url) > 4096
                or set(query)
                != {
                    "versionId",
                    "X-Amz-Algorithm",
                    "X-Amz-Credential",
                    "X-Amz-Date",
                    "X-Amz-Expires",
                    "X-Amz-SignedHeaders",
                    "X-Amz-Signature",
                }
                or any(len(values) != 1 for values in query.values())
                or query.get("versionId") != [reference.object_version]
                or query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]
                or query.get("X-Amz-Expires") != [str(DOWNLOAD_SECONDS)]
                or query.get("X-Amz-SignedHeaders") != ["host"]
            ):
                raise ValueError
            started = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ")
            credential = f"{self.settings.access_key}/{started:%Y%m%d}/us-east-1/s3/aws4_request"
            if (
                query.get("X-Amz-Credential") != [credential]
                or re.fullmatch(r"[a-f0-9]{64}", query["X-Amz-Signature"][0]) is None
            ):
                raise ValueError
            return DownloadGrant(url, started + timedelta(seconds=DOWNLOAD_SECONDS))
        except (BotoCoreError, ClientError, ValueError, KeyError, TypeError):
            raise dependency() from None

    def presign_report_get(self, reference: ReportDownload):
        """Local SigV4 for one CSV report and its exact stored VersionId."""
        validate_report_reference(reference)
        try:
            url = self.signing.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": BUCKET,
                    "Key": reference.key,
                    "VersionId": reference.object_version,
                },
                ExpiresIn=REPORT_DOWNLOAD_SECONDS,
                HttpMethod="GET",
            )
            parts, origin = urlsplit(url), urlsplit(self.settings.public_origin)
            query = parse_qs(parts.query, strict_parsing=True)
            if (
                parts.scheme != origin.scheme
                or parts.netloc != origin.netloc
                or parts.path != f"/{BUCKET}/{reference.key}"
                or parts.fragment
                or len(url) > 4096
                or set(query)
                != {
                    "versionId",
                    "X-Amz-Algorithm",
                    "X-Amz-Credential",
                    "X-Amz-Date",
                    "X-Amz-Expires",
                    "X-Amz-SignedHeaders",
                    "X-Amz-Signature",
                }
                or any(len(values) != 1 for values in query.values())
                or query.get("versionId") != [reference.object_version]
                or query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"]
                or query.get("X-Amz-Expires") != [str(REPORT_DOWNLOAD_SECONDS)]
                or query.get("X-Amz-SignedHeaders") != ["host"]
            ):
                raise ValueError
            started = datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ")
            credential = f"{self.settings.access_key}/{started:%Y%m%d}/us-east-1/s3/aws4_request"
            if (
                query.get("X-Amz-Credential") != [credential]
                or re.fullmatch(r"[a-f0-9]{64}", query["X-Amz-Signature"][0]) is None
            ):
                raise ValueError
            return DownloadGrant(url, started + timedelta(seconds=REPORT_DOWNLOAD_SECONDS))
        except (BotoCoreError, ClientError, ValueError, KeyError, TypeError):
            raise dependency() from None

    @staticmethod
    def _failure(error):
        if isinstance(error, ClientError) and error.response.get("Error", {}).get("Code") in {
            "NoSuchKey",
            "NoSuchVersion",
            "NotFound",
            "404",
        }:
            return ServiceError("OBJECT_NOT_FOUND", 404, "Object version not found")
        return dependency()  # No IAM details, endpoints or provider messages in public errors.

    def inspect_staging(self, tenant_id, upload_id):
        key = staging_key(tenant_id, upload_id)
        try:
            result = self.internal.head_object(Bucket=BUCKET, Key=key)
        except (BotoCoreError, ClientError) as error:
            raise self._failure(error) from None
        version = result.get("VersionId")
        size, mime = result.get("ContentLength"), result.get("ContentType")
        if (
            not isinstance(version, str)
            or not version
            or version == "null"
            or len(version) > 200
            or any(ord(c) < 33 or ord(c) > 126 for c in version)
            or result.get("DeleteMarker") is True
        ):
            raise dependency()  # Versioning must be enabled; never silently pin the latest object.
        if type(size) is not int or size < 1:
            raise ServiceError("IMAGE_INVALID", 422, "Empty or invalid object")
        if size > MAX_BYTES:
            raise ServiceError("IMAGE_TOO_LARGE", 413, "Image exceeds 15 MiB")
        if mime not in MIME_TYPES:
            raise ServiceError("UNSUPPORTED_MEDIA_TYPE", 415, "Unsupported object media type")
        return ObjectVersion(key, version, size, mime)

    def verify_staging_hash(self, tenant_id, upload_id, pinned: ObjectVersion, expected_sha256):
        """Compute the fixed-version digest; this alone never establishes ready."""
        return hashlib.sha256(
            self.read_staging(tenant_id, upload_id, pinned, expected_sha256)
        ).hexdigest()

    def inspect_pinned_staging(self, tenant_id, upload_id, pinned):
        """Replay availability check, outside DB locks; never pin a new/latest version."""
        if (
            pinned.key != staging_key(tenant_id, upload_id)
            or not pinned.version_id
            or pinned.version_id == "null"
            or len(pinned.version_id) > 200
            or type(pinned.size_bytes) is not int
            or not 1 <= pinned.size_bytes <= MAX_BYTES
            or pinned.mime_type not in MIME_TYPES
        ):
            raise ValueError("Invalid pinned staging reference")
        try:
            result = self.internal.head_object(
                Bucket=BUCKET, Key=pinned.key, VersionId=pinned.version_id
            )
        except (BotoCoreError, ClientError, OSError) as error:
            raise self._failure(error) from None
        if (
            result.get("VersionId") != pinned.version_id
            or result.get("ContentLength") != pinned.size_bytes
            or result.get("ContentType") != pinned.mime_type
            or result.get("DeleteMarker") is True
        ):
            raise ServiceError("STATE_CONFLICT", 409, "Pinned input metadata changed")
        return pinned

    def read_staging(self, tenant_id, upload_id, pinned: ObjectVersion, expected_sha256):
        """Return at most 15 MiB only after size, metadata and actual SHA verification."""
        digest(expected_sha256)
        if (
            pinned.key != staging_key(tenant_id, upload_id)
            or not pinned.version_id
            or pinned.version_id == "null"
            or len(pinned.version_id) > 200
            or type(pinned.size_bytes) is not int
            or not 1 <= pinned.size_bytes <= MAX_BYTES
            or pinned.mime_type not in MIME_TYPES
        ):
            raise ValueError("Invalid pinned staging reference")
        stream = None
        try:
            result = self.internal.get_object(
                Bucket=BUCKET, Key=pinned.key, VersionId=pinned.version_id
            )
            stream = result["Body"]
            if (
                result.get("VersionId") != pinned.version_id
                or result.get("ContentLength") != pinned.size_bytes
                or result.get("ContentType") != pinned.mime_type
                or result.get("DeleteMarker") is True
            ):
                raise ServiceError("IMAGE_INVALID", 422, "Pinned object metadata changed")
            checksum, total, chunks = hashlib.sha256(), 0, []
            while chunk := stream.read(64 * 1024):
                total += len(chunk)
                if total > MAX_BYTES or total > pinned.size_bytes:
                    raise ServiceError("IMAGE_INVALID", 422, "Object size does not match grant")
                checksum.update(chunk)
                chunks.append(chunk)
            if total != pinned.size_bytes:
                raise ServiceError("IMAGE_INVALID", 422, "Object stream was truncated")
            actual = checksum.hexdigest()
            if not hmac.compare_digest(actual, expected_sha256):
                raise ServiceError("HASH_MISMATCH", 422, "Object content hash does not match grant")
            return b"".join(chunks)
        except (BotoCoreError, ClientError, OSError) as error:
            raise self._failure(error) from None
        finally:
            if stream is not None:
                stream.close()

    @staticmethod
    def result_version(result):
        version = result.get("VersionId")
        if (
            not isinstance(version, str)
            or not 1 <= len(version) <= 200
            or version == "null"
            or any(not 33 <= ord(c) <= 126 for c in version)
        ):
            raise dependency()
        return version

    def reuse_version(self, key, expected_sha, mime, maximum):
        """Reuse only after hashing exact existing bytes, never ETag or declared metadata.

        Missing destination permits creation. A destination content conflict is a
        server/storage error, not a rejection of the already verified upload.
        """
        digest(expected_sha)
        stream = None
        try:
            try:
                head = self.internal.head_object(Bucket=BUCKET, Key=key)
            except ClientError as error:
                if self._failure(error).code == "OBJECT_NOT_FOUND":
                    return None
                raise
            version = self.result_version(head)
            size = head.get("ContentLength")
            if (
                type(size) is not int
                or not 1 <= size <= maximum
                or head.get("ContentType") != mime
                or head.get("DeleteMarker") is True
            ):
                raise ServiceError("INTERNAL_ERROR", 500, "Stored object conflict")
            result = self.internal.get_object(Bucket=BUCKET, Key=key, VersionId=version)
            stream = result["Body"]
            if (
                result.get("VersionId") != version
                or result.get("ContentLength") != size
                or result.get("ContentType") != mime
                or result.get("DeleteMarker") is True
            ):
                raise ServiceError("INTERNAL_ERROR", 500, "Stored object conflict")
            checksum, total = hashlib.sha256(), 0
            while chunk := stream.read(64 * 1024):
                total += len(chunk)
                if total > size or total > maximum:
                    raise ServiceError("INTERNAL_ERROR", 500, "Stored object conflict")
                checksum.update(chunk)
            if total != size or not hmac.compare_digest(checksum.hexdigest(), expected_sha):
                raise ServiceError("INTERNAL_ERROR", 500, "Stored object conflict")
            return version
        except (BotoCoreError, ClientError, OSError):
            raise dependency() from None
        finally:
            if stream is not None:
                stream.close()

    def preserve_original(self, pinned, key, expected_sha):
        """Copy exactly the verified staging version; never copy the latest key."""
        existing = self.reuse_version(key, expected_sha, pinned.mime_type, MAX_BYTES)
        if existing is not None:
            return existing
        try:
            result = self.internal.copy_object(
                Bucket=BUCKET,
                Key=key,
                CopySource={"Bucket": BUCKET, "Key": pinned.key, "VersionId": pinned.version_id},
                MetadataDirective="REPLACE",
                ContentType=pinned.mime_type,
            )
            return self.result_version(result)
        except (BotoCoreError, ClientError, OSError) as error:
            raise self._failure(error) from None

    def put_analysis(self, key, payload):
        if not 1 <= len(payload) <= MAX_ANALYSIS_BYTES:
            raise ServiceError("INTERNAL_ERROR", 500, "Analysis encoding exceeds limit")
        existing = self.reuse_version(
            key, hashlib.sha256(payload).hexdigest(), "image/png", MAX_ANALYSIS_BYTES
        )
        if existing is not None:
            return existing
        try:
            result = self.internal.put_object(
                Bucket=BUCKET, Key=key, Body=payload, ContentType="image/png"
            )
            return self.result_version(result)
        except (BotoCoreError, ClientError, OSError) as error:
            raise self._failure(error) from None

    def read_analysis(self, tenant, laboratory, reference):
        """Read the frozen canonical version, bound bytes and verify actual SHA."""
        for value in (tenant, laboratory, reference["image_id"]):
            identifier(value)
        digest(reference["sha256"])
        expected = (
            f"tenant/{tenant}/lab/{laboratory}/analysis/{reference['image_id']}/"
            f"{reference['sha256']}.png"
        )
        version = self.result_version({"VersionId": reference["object_version"]})
        if reference["object_key"] != expected or reference["mime_type"] != "image/png":
            raise ServiceError("SCHEMA_MISMATCH", 502, "Invalid frozen analysis reference")
        stream = None
        try:
            result = self.internal.get_object(Bucket=BUCKET, Key=expected, VersionId=version)
            stream = result["Body"]
            size = result.get("ContentLength")
            if (
                result.get("VersionId") != version
                or type(size) is not int
                or not 1 <= size <= MAX_ANALYSIS_BYTES
                or result.get("ContentType") != "image/png"
                or result.get("DeleteMarker") is True
                or result.get("ContentEncoding")
            ):
                raise ServiceError("IMAGE_INVALID", 422, "Frozen analysis metadata differs")
            chunks, checksum, total = [], hashlib.sha256(), 0
            while chunk := stream.read(min(64 * 1024, size - total + 1)):
                total += len(chunk)
                if total > size:
                    raise ServiceError("IMAGE_INVALID", 422, "Analysis size differs")
                checksum.update(chunk)
                chunks.append(chunk)
            if total != size:
                raise ServiceError("IMAGE_INVALID", 422, "Analysis stream was truncated")
            if not hmac.compare_digest(checksum.hexdigest(), reference["sha256"]):
                raise ServiceError("HASH_MISMATCH", 422, "Analysis hash differs")
            return b"".join(chunks)
        except (BotoCoreError, ClientError, OSError) as error:
            raise self._failure(error) from None
        finally:
            if stream is not None:
                stream.close()

    def put_derivative(self, key, payload):
        """Canonical D object, deterministic bytes, explicit non-null VersionId."""
        from packages.domain.inference_evidence import derivative_key
        from packages.inference_protocol.evidence import MAX_CROP_BYTES

        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_CROP_BYTES:
            raise ServiceError("INTERNAL_ERROR", 500, "Crop encoding exceeds limit")
        parts = key.split("/")
        sha = hashlib.sha256(payload).hexdigest()
        if len(parts) != 8 or key != derivative_key(parts[1], parts[3], parts[5], parts[6], sha):
            raise ServiceError("SCHEMA_MISMATCH", 502, "Invalid derivative namespace")
        existing = self.reuse_version(key, sha, "image/png", MAX_CROP_BYTES)
        if existing is not None:
            return existing
        try:
            result = self.internal.put_object(
                Bucket=BUCKET, Key=key, Body=payload, ContentType="image/png"
            )
            return self.result_version(result)
        except (BotoCoreError, ClientError, OSError) as error:
            raise self._failure(error) from None

    def put_report(self, source, payload):
        """Upload deterministic CSV bytes; register only an explicit VersionId."""
        if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_REPORT_BYTES:
            raise ServiceError("INTERNAL_ERROR", 500, "Report encoding exceeds limit")
        checksum = hashlib.sha256(payload).hexdigest()
        key = report_key(source, checksum)
        version = self.reuse_version(key, checksum, REPORT_MIME, MAX_REPORT_BYTES)
        if version is None:
            try:
                result = self.internal.put_object(
                    Bucket=BUCKET, Key=key, Body=payload, ContentType=REPORT_MIME
                )
                version = self.result_version(result)
            except (BotoCoreError, ClientError, OSError) as error:
                raise self._failure(error) from None
        return artifact_for(source, payload, version)
