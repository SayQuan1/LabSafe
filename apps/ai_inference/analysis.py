"""Read-only, fixed-version analysis PNG input; no SDK credential provider chain."""

import hashlib
import hmac
import http.client
import ipaddress
import math
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

from apps.ai_inference.adapters.dfine import AdapterError

MAX_ANALYSIS_BYTES = 128 * 1024 * 1024
BUCKET = "labsafe-private"
REGION = "us-east-1"


def _uuid(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise AdapterError("UNAUTHORIZED_REF", "Invalid analysis namespace") from None


def _endpoint(value):
    try:
        parsed = urlsplit(value)
        if (
            not isinstance(value, str)
            or any(c.isspace() for c in value)
            or parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or any(c in value for c in ("?", "#", "\\"))
        ):
            raise ValueError
        parsed.port
        if parsed.scheme == "http":
            # Plain HTTP is a local development transport only.
            if (
                parsed.hostname != "localhost"
                and not ipaddress.ip_address(parsed.hostname).is_loopback
            ):
                raise ValueError
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise AdapterError("VALIDATION_ERROR", "Invalid analysis endpoint configuration") from None


@dataclass(frozen=True)
class AnalysisSettings:
    endpoint: str
    allowed_endpoints: tuple[str, ...]
    allowed_tenants: frozenset[str]
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)

    def validate(self):
        _endpoint(self.endpoint)
        if (
            not isinstance(self.allowed_endpoints, tuple)
            or not self.allowed_endpoints
            or self.endpoint not in self.allowed_endpoints
            or not isinstance(self.allowed_tenants, frozenset)
            or not self.allowed_tenants
        ):
            raise AdapterError(
                "VALIDATION_ERROR", "Explicit analysis endpoint and tenant allowlists required"
            )
        for endpoint in self.allowed_endpoints:
            _endpoint(endpoint)
        for tenant in self.allowed_tenants:
            _uuid(tenant)
        for secret in (self.access_key, self.secret_key):
            if (
                not isinstance(secret, str)
                or not 1 <= len(secret) <= 256
                or any(not 33 <= ord(c) <= 126 for c in secret)
            ):
                raise AdapterError("VALIDATION_ERROR", "Invalid analysis credentials")

    @classmethod
    def from_env(cls):
        try:
            settings = cls(
                os.environ["AI_S3_ENDPOINT"],
                tuple(os.environ["AI_S3_ALLOWED_ENDPOINTS"].split(",")),
                frozenset(os.environ["AI_ALLOWED_TENANTS"].split(",")),
                Path(os.environ["AI_S3_ACCESS_KEY_FILE"])
                .read_text(encoding="ascii")
                .rstrip("\r\n"),
                Path(os.environ["AI_S3_SECRET_KEY_FILE"])
                .read_text(encoding="ascii")
                .rstrip("\r\n"),
            )
            settings.validate()
            return settings
        except (KeyError, OSError, UnicodeError):
            raise AdapterError("VALIDATION_ERROR", "Analysis configuration unavailable") from None


def verify_reference(reference, tenant, laboratory, allowed_tenants):
    if not isinstance(reference, dict):
        raise AdapterError("UNAUTHORIZED_REF", "Invalid analysis reference")
    for value in (tenant, laboratory, reference.get("image_id")):
        _uuid(value)
    if tenant not in allowed_tenants:
        raise AdapterError("FORBIDDEN", "Tenant is not allowed")
    sha = reference.get("sha256")
    version = reference.get("object_version")
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        raise AdapterError("UNAUTHORIZED_REF", "Invalid analysis digest")
    if (
        not isinstance(version, str)
        or not 1 <= len(version) <= 200
        or version == "null"
        or any(not 33 <= ord(c) <= 126 for c in version)
    ):
        raise AdapterError("UNAUTHORIZED_REF", "An exact analysis version is required")
    key = f"tenant/{tenant}/lab/{laboratory}/analysis/{reference['image_id']}/{sha}.png"
    if reference.get("object_key") != key:
        raise AdapterError(
            "UNAUTHORIZED_REF", "Only canonical analysis PNG references are accepted"
        )
    if reference.get("mime_type") != "image/png":
        raise AdapterError("VALIDATION_ERROR", "Analysis images must be normalized PNG")


def signed_get(settings, key, version, instant):
    """S3 SigV4 with one fixed VersionId and only a GET operation."""
    parsed = _endpoint(settings.endpoint)
    path = f"/{BUCKET}/" + quote(key, safe="/")
    query = "versionId=" + quote(version, safe="")
    stamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date = stamp[:8]
    empty = hashlib.sha256(b"").hexdigest()
    headers = {"host": parsed.netloc, "x-amz-content-sha256": empty, "x-amz-date": stamp}
    names = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical = "\n".join(("GET", path, query, canonical_headers, names, empty))
    scope = f"{date}/{REGION}/s3/aws4_request"
    to_sign = "\n".join(
        ("AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest())
    )
    key_bytes = ("AWS4" + settings.secret_key).encode("ascii")
    for part in (date, REGION, "s3", "aws4_request"):
        key_bytes = hmac.new(key_bytes, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key_bytes, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={settings.access_key}/{scope}, "
        f"SignedHeaders={names}, Signature={signature}"
    )
    return path + "?" + query, headers


def _remaining(deadline):
    if type(deadline) not in (float, int) or not math.isfinite(deadline):
        raise AdapterError("VALIDATION_ERROR", "Invalid analysis deadline")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AdapterError("AI_TIMEOUT", "Analysis deadline exceeded")
    return remaining


def decode_analysis(raw, expected_sha256):
    from packages.image_evidence.analysis import decode_analysis as decode
    from packages.image_evidence.perspective import CropError

    try:
        return decode(raw, expected_sha256)
    except CropError as error:
        raise AdapterError(error.code, str(error)) from None


class AnalysisReader:
    def __init__(self, settings):
        settings.validate()
        self.settings = settings

    def read(self, reference, tenant, laboratory, deadline):
        verify_reference(reference, tenant, laboratory, self.settings.allowed_tenants)
        parsed = _endpoint(self.settings.endpoint)
        remaining = _remaining(deadline)
        timeout = min(2, remaining)
        deadline_limited = remaining <= 2
        if parsed.scheme == "https":
            connection = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        try:
            path, headers = signed_get(
                self.settings,
                reference["object_key"],
                reference["object_version"],
                datetime.now(timezone.utc),
            )
            # Direct configured connection: no proxy env, URL opener or redirect handler.
            connection.connect()
            transport = connection.sock
            remaining = _remaining(deadline)
            deadline_limited = remaining <= 5
            transport.settimeout(min(5, remaining))
            connection.request("GET", path, headers=headers)
            with connection.getresponse() as response:
                _remaining(deadline)
                if response.status == 404:
                    raise AdapterError("OBJECT_NOT_FOUND", "Analysis object version not found")
                if response.status != 200:
                    raise AdapterError("DEPENDENCY_UNAVAILABLE", "Analysis object unavailable")
                for name in ("Content-Length", "Content-Type", "x-amz-version-id"):
                    if len(response.headers.get_all(name, [])) != 1:
                        raise AdapterError(
                            "SCHEMA_MISMATCH", "Analysis response headers are invalid"
                        )
                if response.getheader("x-amz-version-id") != reference["object_version"]:
                    raise AdapterError("HASH_MISMATCH", "Analysis object version differs")
                if (
                    response.getheader("Content-Type") != "image/png"
                    or response.getheader("Transfer-Encoding") is not None
                    or response.getheader("Content-Encoding") not in (None, "identity")
                ):
                    raise AdapterError("SCHEMA_MISMATCH", "Analysis response encoding is invalid")
                length = response.getheader("Content-Length")
                if len(length) > 10 or re.fullmatch(r"[0-9]+", length) is None:
                    raise AdapterError("SCHEMA_MISMATCH", "Analysis content length is invalid")
                length = int(length)
                if not 1 <= length <= MAX_ANALYSIS_BYTES:
                    raise AdapterError("IMAGE_TOO_LARGE", "Analysis byte limit exceeded")
                raw, digest = bytearray(), hashlib.sha256()
                while len(raw) < length:
                    remaining = _remaining(deadline)
                    deadline_limited = remaining <= 5
                    # HTTP/1.0 may detach connection.sock while the response still
                    # owns its file. Retain the transport to bound every body read.
                    transport.settimeout(min(5, remaining))
                    chunk = response.read1(min(65536, length - len(raw)))
                    _remaining(deadline)
                    if not chunk:
                        raise AdapterError("IMAGE_INVALID", "Analysis response is truncated")
                    raw.extend(chunk)
                    digest.update(chunk)
                if digest.hexdigest() != reference["sha256"]:
                    raise AdapterError("HASH_MISMATCH", "Analysis content hash differs")
                return raw
        except AdapterError:
            raise
        except TimeoutError:
            code = (
                "AI_TIMEOUT"
                if deadline_limited or time.monotonic() >= deadline
                else "DEPENDENCY_UNAVAILABLE"
            )
            raise AdapterError(code, "Analysis read timed out") from None
        except (OSError, http.client.HTTPException):
            raise AdapterError("DEPENDENCY_UNAVAILABLE", "Analysis read failed") from None
        finally:
            connection.close()

    def decode(self, reference, tenant, laboratory, deadline):
        raw = self.read(reference, tenant, laboratory, deadline)
        image = decode_analysis(raw, reference["sha256"])
        try:
            _remaining(deadline)
        except AdapterError:
            image.close()
            raise
        return image, reference["sha256"]
