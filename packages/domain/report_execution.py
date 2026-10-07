"""Immutable report execution values and deterministic, bounded CSV encoding."""

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from itertools import chain

from packages.domain.job_execution import LeaseLost
from packages.domain.reports import COLUMNS, MAX_FINDINGS
from packages.domain.uploads import capture_time, digest, identifier

MAX_REPORT_BYTES = 64 * 1024 * 1024
REPORT_SECONDS = 120
REPORT_MIME = "text/csv; charset=utf-8"
REPORT_RETRYABLE = {"DEPENDENCY_UNAVAILABLE", "STAGE_TIMEOUT"}
REPORT_ERRORS = REPORT_RETRYABLE | {"INTERNAL_ERROR", "SCHEMA_MISMATCH"}


class ReportLeaseLost(LeaseLost):
    pass


class ReportInvalid(ValueError):
    def __init__(self):
        super().__init__("Invalid frozen report snapshot")


@dataclass(frozen=True)
class ReportInput:
    export_id: str
    tenant_id: str
    requested_by: str
    format: str
    filters: str
    snapshot_at: str
    snapshot: str
    version: int


@dataclass(frozen=True)
class ReportLease:
    tenant_id: str
    task_id: str
    owner: str
    token: int
    generation: int
    attempt: int
    input: ReportInput


@dataclass(frozen=True)
class ReportArtifact:
    key: str
    checksum: str
    object_version: str
    size_bytes: int


def frozen_snapshot(source):
    try:
        identifier(source.tenant_id)
        identifier(source.export_id)
        value, filters = json.loads(source.snapshot), json.loads(source.filters)
        labs = filters["laboratory_ids"]
        if (
            source.format != "csv"
            or not isinstance(labs, list)
            or not 1 <= len(labs) <= 100
            or any(not isinstance(lab, str) for lab in labs)
            or labs != sorted(set(labs))
            or not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "export_id",
                "tenant_id",
                "laboratory_ids",
                "snapshot_at",
                "columns",
                "rows",
            }
            or value["schema_version"] != "1"
            or value["tenant_id"] != source.tenant_id
            or value["export_id"] != source.export_id
            or value["laboratory_ids"] != labs
            or value["snapshot_at"] != source.snapshot_at
            or value["columns"] != list(COLUMNS)
            or not isinstance(value["rows"], list)
        ):
            raise ReportInvalid()
        capture_time(source.snapshot_at)
        for lab in labs:
            identifier(lab)
        findings = 0
        for row in value["rows"]:
            if (
                not isinstance(row, dict)
                or set(row) != set(COLUMNS)
                or any(v is not None and not isinstance(v, str) for v in row.values())
                or row["laboratory_id"] not in labs
                or row["snapshot_at"] != source.snapshot_at
            ):
                raise ReportInvalid()
            findings += row["finding_id"] is not None
        if findings > MAX_FINDINGS:
            raise ReportInvalid()
        return value
    except Exception as error:
        if isinstance(error, ReportInvalid):
            raise
        raise ReportInvalid() from None


def csv_bytes(source, checkpoint=lambda: None):
    snapshot = frozen_snapshot(source)
    output = io.BytesIO()
    output.write(b"\xef\xbb\xbf")
    line = io.StringIO(newline="")
    writer = csv.writer(line, lineterminator="\r\n")
    for row in chain((COLUMNS,), (tuple(r[c] for c in COLUMNS) for r in snapshot["rows"])):
        checkpoint()
        line.seek(0)
        line.truncate(0)
        writer.writerow(
            [
                "'" + v
                if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                else v
                for v in row
            ]
        )
        encoded = line.getvalue().encode("utf-8")
        if output.tell() + len(encoded) > MAX_REPORT_BYTES:
            raise ReportInvalid()
        output.write(encoded)
    return output.getvalue()


def report_key(source, checksum):
    snapshot = frozen_snapshot(source)
    digest(checksum)
    return (
        f"tenant/{source.tenant_id}/lab/{snapshot['laboratory_ids'][0]}/"
        f"reports/{source.export_id}/{checksum}.csv"
    )


def artifact_for(source, payload, object_version):
    checksum = hashlib.sha256(payload).hexdigest()
    result = ReportArtifact(report_key(source, checksum), checksum, object_version, len(payload))
    validate_artifact(source, result)
    return result


def validate_artifact(source, artifact):
    if (
        artifact.key != report_key(source, artifact.checksum)
        or not isinstance(artifact.object_version, str)
        or not 1 <= len(artifact.object_version) <= 200
        or artifact.object_version == "null"
        or any(not 33 <= ord(c) <= 126 for c in artifact.object_version)
        or type(artifact.size_bytes) is not int
        or not 1 <= artifact.size_bytes <= MAX_REPORT_BYTES
    ):
        raise ReportInvalid()
