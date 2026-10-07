"""Authorized, fixed-version report download references."""

from dataclasses import dataclass

from packages.domain.report_execution import MAX_REPORT_BYTES, REPORT_MIME
from packages.domain.security import Permission, ServiceError, authorize, not_found
from packages.domain.uploads import digest, identifier

REPORT_DOWNLOAD_SECONDS = 60


@dataclass(frozen=True)
class ReportDownload:
    tenant_id: str
    export_id: str
    laboratory_ids: tuple[str, ...]
    checksum: str
    key: str
    object_version: str
    size_bytes: int
    mime_type: str


def validate_reference(reference: ReportDownload) -> None:
    if not isinstance(reference, ReportDownload):
        raise ValueError("Invalid report download reference")
    identifier(reference.tenant_id)
    identifier(reference.export_id)
    labs = reference.laboratory_ids
    if not isinstance(labs, tuple) or not 1 <= len(labs) <= 100 or tuple(sorted(set(labs))) != labs:
        raise ValueError("Invalid report scope")
    for lab in labs:
        identifier(lab)
    digest(reference.checksum)
    expected = (
        f"tenant/{reference.tenant_id}/lab/{labs[0]}/reports/"
        f"{reference.export_id}/{reference.checksum}.csv"
    )
    if reference.key != expected:
        raise ValueError("Invalid report download key")
    version = reference.object_version
    if (
        not isinstance(version, str)
        or not 1 <= len(version) <= 200
        or version == "null"
        or any(not 33 <= ord(c) <= 126 for c in version)
    ):
        raise ValueError("Invalid report download version")
    if type(reference.size_bytes) is not int or not 1 <= reference.size_bytes <= MAX_REPORT_BYTES:
        raise ValueError("Invalid report download size")
    if reference.mime_type != REPORT_MIME:
        raise ValueError("Invalid report download media type")


def download_reference(actor, row, now):
    if row is None or row["tenant_id"] != actor.tenant_id:
        raise not_found()
    try:
        import json

        filters = json.loads(row["filters"]) if isinstance(row["filters"], str) else row["filters"]
        labs = filters["laboratory_ids"]
    except (TypeError, ValueError, KeyError):
        raise ServiceError(
            "STATE_CONFLICT", 409, "Report download evidence is incomplete"
        ) from None
    if not isinstance(labs, list) or any(not isinstance(lab, str) for lab in labs):
        raise ServiceError("STATE_CONFLICT", 409, "Report download evidence is incomplete")
    if actor.user_id != row["requested_by"]:
        for lab in labs:
            authorize(actor, Permission.ADMIN, lab)
    if row["status"] != "ready" or row["format"] != "csv":
        raise ServiceError("STATE_CONFLICT", 409, "Report is not ready for download")
    if row["expires_at"] is None or row["expires_at"] <= now:
        raise ServiceError("STATE_CONFLICT", 409, "Report download has expired")
    reference = ReportDownload(
        row["tenant_id"],
        row["id"],
        tuple(labs),
        row["checksum"],
        row["object_key"],
        row["object_version"],
        row["size_bytes"],
        REPORT_MIME,
    )
    try:
        validate_reference(reference)
    except (ValueError, TypeError):
        raise ServiceError(
            "STATE_CONFLICT", 409, "Report download evidence is incomplete"
        ) from None
    return reference
