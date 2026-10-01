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

from packages.domain.security import ServiceError
from packages.domain.uploads import GRANT_SECONDS, MAX_BYTES, MIME_TYPES, digest, staging_key

BUCKET = "labsafe-private"


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
    def from_environment(cls, public_origin):
        try:

            def secret(name):
                # No provider chain, ~/.aws, environment credential fallback or metadata service.
                return Path(os.environ[name]).read_text(encoding="ascii").rstrip("\r\n")

            return cls(
                internal_endpoint=os.environ["S3_ENDPOINT"],
                public_endpoint=os.environ["S3_PUBLIC_ENDPOINT"],
                public_origin=public_origin,
                access_key=secret("S3_ACCESS_KEY_FILE"),
                secret_key=secret("S3_SECRET_KEY_FILE"),
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
        """Future general-worker primitive, NOT image decoding or a ready-state decision."""
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
            checksum, total = hashlib.sha256(), 0
            while chunk := stream.read(64 * 1024):
                total += len(chunk)
                if total > MAX_BYTES or total > pinned.size_bytes:
                    raise ServiceError("IMAGE_INVALID", 422, "Object size does not match grant")
                checksum.update(chunk)
            if total != pinned.size_bytes:
                raise ServiceError("IMAGE_INVALID", 422, "Object stream was truncated")
            actual = checksum.hexdigest()
            if not hmac.compare_digest(actual, expected_sha256):
                raise ServiceError("HASH_MISMATCH", 422, "Object content hash does not match grant")
            return actual
        except (BotoCoreError, ClientError, OSError) as error:
            raise self._failure(error) from None
        finally:
            if stream is not None:
                stream.close()
