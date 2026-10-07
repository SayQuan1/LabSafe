"""Export request bounds and fixed CSV snapshot columns."""

from datetime import timedelta

from packages.domain.security import ServiceError
from packages.domain.uploads import capture_time, identifier

MAX_FINDINGS = 10000
COLUMNS = (
    "inspection_id",
    "item_id",
    "laboratory_id",
    "location_id",
    "run_id",
    "fact_revision_id",
    "rule_bundle_id",
    "finding_id",
    "rule_id",
    "severity",
    "status",
    "explanation",
    "task_status",
    "review_outcome",
    "snapshot_at",
)


def validate_request(body):
    def invalid():
        return ServiceError("VALIDATION_ERROR", 422, "Invalid export range; narrow the filters")

    if not isinstance(body, dict) or set(body) != {"format", "filters"}:
        raise invalid()
    if body["format"] not in ("csv", "pdf"):
        raise invalid()
    filters = body["filters"]
    if not isinstance(filters, dict) or set(filters) != {
        "laboratory_ids",
        "from",
        "to",
        "severity",
        "finding_status",
    }:
        raise invalid()
    labs = filters["laboratory_ids"]
    if not isinstance(labs, list) or not 1 <= len(labs) <= 100:
        raise invalid()
    for lab in labs:
        identifier(lab)
    if len(set(labs)) != len(labs):
        raise invalid()
    for name, allowed in (
        ("severity", {"low", "medium", "high", "critical"}),
        (
            "finding_status",
            {"needs_review", "confirmed", "rejected", "cannot_determine", "dispatched", "closed"},
        ),
    ):
        values = filters[name]
        if not isinstance(values, list) or len(values) > len(allowed):
            raise invalid()
        if any(not isinstance(value, str) or value not in allowed for value in values):
            raise invalid()
        if len(set(values)) != len(values):
            raise invalid()
    start, end = capture_time(filters["from"]), capture_time(filters["to"])
    if end < start or end - start > timedelta(days=31):
        raise invalid()
    normalized = {
        **filters,
        "laboratory_ids": sorted(labs),
        "from": start.isoformat(timespec="milliseconds") + "Z",
        "to": end.isoformat(timespec="milliseconds") + "Z",
    }
    return normalized, start, end
