"""Pure upload guards. Server-loaded ownership, never client-supplied scope."""

import math
import re
from datetime import datetime, timezone
from uuid import UUID

from packages.domain.security import Permission, ServiceError, authorize, not_found
from packages.domain.workflow import Item, Task

MAX_BYTES = 15 * 1024 * 1024
GRANT_SECONDS = 600
MAX_OPEN_GRANTS = 10
MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
CAPTURE_STATES = frozenset({"draft", "uploaded", "needs_retake", "needs_review", "failed"})
EVIDENCE_STATES = frozenset({"in_progress", "rejected"})


def invalid(message="Invalid upload request"):
    return ServiceError("VALIDATION_ERROR", 422, message)


def identifier(value):
    try:
        if not isinstance(value, str) or len(value) != 36 or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise invalid("Invalid canonical identifier") from None
    return value


def digest(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise invalid("Invalid SHA256")
    return value


def capture_time(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
            r"(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
            value,
        )
        is None
    ):
        raise invalid("Capture time must be RFC3339 with an explicit offset")
    try:
        parsed = datetime.fromisoformat(value.upper().replace("Z", "+00:00"))
        utc = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        if utc.year < 1000:
            raise ValueError
    except (ValueError, OverflowError):
        raise invalid("Capture time is outside the database range") from None
    # The contract does not define a recency/skew window. Do not invent one.
    return utc.replace(microsecond=utc.microsecond // 1000 * 1000)


def validate_upload_request(body):
    fields = {
        "owner_type",
        "owner_id",
        "filename",
        "mime_type",
        "size_bytes",
        "sha256",
        "captured_at",
    }
    if not isinstance(body, dict) or set(body) != fields:
        raise invalid()
    if body["owner_type"] not in ("inspection_item", "remediation_task"):
        raise invalid("Invalid upload owner type")
    identifier(body["owner_id"])
    name = body["filename"]
    if (
        not isinstance(name, str)
        or not name.strip()
        or len(name) > 255
        or name in {".", ".."}
        or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)
    ):
        raise invalid("Filename must be a display name, not a path")
    if not isinstance(body["mime_type"], str) or body["mime_type"] not in MIME_TYPES:
        raise ServiceError("UNSUPPORTED_MEDIA_TYPE", 415, "Unsupported image media type")
    if type(body["size_bytes"]) is not int or body["size_bytes"] < 1:
        raise invalid("Invalid image size")
    if body["size_bytes"] > MAX_BYTES:
        raise ServiceError("IMAGE_TOO_LARGE", 413, "Image exceeds 15 MiB")
    digest(body["sha256"])
    return capture_time(body["captured_at"])


def authorize_upload(actor, owner: Item | Task, *, check_state=True):
    if actor.tenant_id != owner.tenant_id:
        raise not_found()
    if isinstance(owner, Item):
        authorize(actor, Permission.CAPTURE, owner.laboratory_id, creator_id=owner.creator_id)
        valid = (
            owner.inspection_status in {"draft", "in_progress"} and owner.status in CAPTURE_STATES
        )
    elif isinstance(owner, Task):
        authorize(actor, Permission.ASSIGNEE, owner.laboratory_id, assignee_id=owner.assignee_id)
        valid = owner.status in EVIDENCE_STATES
    else:
        raise ValueError("Unsupported server upload owner")
    if check_state and not valid:
        raise ServiceError("STATE_CONFLICT", 409, "Owner cannot accept capture")


def require_grant_capacity(expirations, now):
    # Caller locks the user quota mutex BEFORE authentication/foreign-key shared reads.
    active = [expires for expires in expirations if expires > now]
    if len(active) >= MAX_OPEN_GRANTS:
        raise ServiceError(
            "RATE_LIMITED",
            429,
            "Too many unfinished upload grants",
            retry_after=max(1, math.ceil((min(active) - now).total_seconds())),
        )


def staging_key(tenant_id, upload_id):
    return f"staging/{identifier(tenant_id)}/{identifier(upload_id)}"


def validate_complete_request(body):
    if not isinstance(body, dict) or set(body) != {"upload_id", "sha256"}:
        raise invalid("Invalid upload completion request")
    identifier(body["upload_id"])
    digest(body["sha256"])


def owner_reference(row):
    item, task = row["inspection_item_id"], row["remediation_task_id"]
    if (item is None) == (task is None):
        raise RuntimeError("Invalid persisted upload owner")
    return ("inspection_item", item) if item is not None else ("remediation_task", task)


def require_completion(upload, sha256, now, *, already_completed=False):
    if upload["expected_sha256"] != sha256:
        raise ServiceError("HASH_MISMATCH", 422, "Completion hash differs from upload grant")
    if upload["object_key"] != staging_key(upload["tenant_id"], upload["id"]):
        raise RuntimeError("Invalid persisted staging reference")
    if already_completed:
        if upload["status"] not in {"validating", "ready", "rejected"}:
            raise RuntimeError("Completed image has inconsistent upload status")
        return
    if upload["status"] != "granted" or upload["expires_at"] <= now:
        raise ServiceError("STATE_CONFLICT", 409, "Upload grant is no longer completable")


def require_staging_match(upload, pinned):
    # This validates HEAD metadata only; the worker must still hash/decode the bytes.
    version = pinned.version_id
    if (
        not isinstance(version, str)
        or not version
        or version == "null"
        or len(version) > 200
        or any(not 33 <= ord(c) <= 126 for c in version)
        or pinned.key != upload["object_key"]
        or type(pinned.size_bytes) is not int
        or pinned.size_bytes != upload["size_bytes"]
        or not 1 <= pinned.size_bytes <= MAX_BYTES
        or pinned.mime_type != upload["mime_type"]
        or pinned.mime_type not in MIME_TYPES
    ):
        raise ServiceError("IMAGE_INVALID", 422, "Staging object does not match upload grant")
