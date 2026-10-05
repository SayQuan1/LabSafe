"""Authorized, fixed-version O/A download references; never staging or arbitrary keys."""

from dataclasses import dataclass

from packages.domain.image_validation import EXTENSIONS
from packages.domain.security import Permission, ServiceError, authorize, not_found
from packages.domain.uploads import digest, identifier

DOWNLOAD_SECONDS = 60


@dataclass(frozen=True)
class ImageDownload:
    tenant_id: str
    laboratory_id: str
    image_id: str
    variant: str
    sha256: str
    mime_type: str
    key: str
    object_version: str


def validate_reference(reference):
    if not isinstance(reference, ImageDownload):
        raise ValueError("Invalid image download reference")
    for value in (reference.tenant_id, reference.laboratory_id, reference.image_id):
        identifier(value)
    digest(reference.sha256)
    if reference.variant not in {"analysis", "original"} or reference.mime_type not in EXTENSIONS:
        raise ValueError("Invalid image download variant or media type")
    if reference.variant == "analysis" and reference.mime_type != "image/png":
        raise ValueError("Analysis must be PNG")
    root = f"tenant/{reference.tenant_id}/lab/{reference.laboratory_id}"
    expected = (
        f"{root}/{reference.variant}/{reference.image_id}/"
        f"{reference.sha256}.{EXTENSIONS[reference.mime_type]}"
    )
    if reference.key != expected:
        raise ValueError("Invalid image download key")
    version = reference.object_version
    if (
        not isinstance(version, str)
        or not 1 <= len(version) <= 200
        or version == "null"
        or any(not 33 <= ord(c) <= 126 for c in version)
    ):
        raise ValueError("Invalid image download version")


def download_reference(actor, row, variant):
    if variant not in {"analysis", "original"}:
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid download variant")
    if row is None or row["tenant_id"] != actor.tenant_id:
        raise not_found()
    authorize(actor, Permission.READ, row["laboratory_id"])
    if variant == "original":
        authorize(actor, Permission.ADMIN)
    if row["status"] != "ready":
        raise ServiceError("STATE_CONFLICT", 409, "Image is not ready for download")
    reference = ImageDownload(
        row["tenant_id"],
        row["laboratory_id"],
        row["id"],
        variant,
        row[f"{variant}_sha256"],
        "image/png" if variant == "analysis" else row["mime_type"],
        row[f"{variant}_key"],
        row[f"{variant}_object_version"],
    )
    try:
        validate_reference(reference)
    except (ValueError, TypeError):
        raise ServiceError("STATE_CONFLICT", 409, "Image download evidence is incomplete") from None
    return reference
