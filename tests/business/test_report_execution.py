"""Frozen CSV encoding, pinned S3 artifacts and bounded worker orchestration."""

import csv
import hashlib
import io
import json
import multiprocessing
import time
from dataclasses import replace
from pathlib import Path
from threading import Event, Timer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from apps.worker.report_export import BoundedReportPrepare
from packages.application.job_execution import LeaseHeartbeat
from packages.application.report_execution import ReportExecution
from packages.domain.job_execution import LeaseLost
from packages.domain.report_execution import (
    REPORT_ERRORS,
    REPORT_MIME,
    REPORT_RETRYABLE,
    ReportInput,
    ReportInvalid,
    artifact_for,
    csv_bytes,
    report_key,
    validate_artifact,
)
from packages.domain.reports import COLUMNS
from packages.domain.security import ServiceError
from packages.shared.json_hash import canonical_json
from packages.storage.s3 import BUCKET, S3Settings, S3Storage
from tests.business.test_s3_storage import settings


def source(text="说明,含逗号\n第二行"):
    tenant, export, lab = str(uuid4()), str(uuid4()), str(uuid4())
    instant = "2026-10-06T00:00:00.000Z"
    row = dict.fromkeys(COLUMNS)
    row.update(laboratory_id=lab, snapshot_at=instant, explanation=text)
    snapshot = {
        "schema_version": "1",
        "tenant_id": tenant,
        "export_id": export,
        "laboratory_ids": [lab],
        "snapshot_at": instant,
        "columns": list(COLUMNS),
        "rows": [row],
    }
    return ReportInput(
        export,
        tenant,
        str(uuid4()),
        "csv",
        canonical_json({"laboratory_ids": [lab]}),
        instant,
        canonical_json(snapshot),
        2,
    )


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_report_csv_formula_prefixes_are_escaped(prefix):
    value = source(prefix + '中文,"value"\nnext')
    encoded = csv_bytes(value)
    assert encoded.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(encoded.decode("utf-8-sig"), newline="")))
    assert rows[0] == list(COLUMNS)
    assert rows[1][COLUMNS.index("explanation")] == "'" + prefix + '中文,"value"\nnext'
    assert rows[1][0] == ""
    assert csv_bytes(value) == encoded


def test_report_csv_quotes_unicode_newlines_and_header_only_snapshot():
    value = source()
    rows = list(csv.reader(io.StringIO(csv_bytes(value).decode("utf-8-sig"), newline="")))
    assert rows[1][COLUMNS.index("explanation")] == "说明,含逗号\n第二行"
    frozen = json.loads(value.snapshot)
    frozen["rows"] = []
    empty = replace(value, snapshot=canonical_json(frozen))
    assert csv_bytes(empty).decode("utf-8-sig") == ",".join(COLUMNS) + "\r\n"


@pytest.mark.parametrize(
    "fault",
    [
        "tenant",
        "export",
        "labs",
        "columns",
        "time",
        "row_scope",
        "row_value",
        "extra",
        "json",
        "pdf",
    ],
)
def test_report_invalid_snapshot_cannot_generate_bytes(fault):
    value = source()
    frozen = json.loads(value.snapshot)
    if fault in {"tenant", "export"}:
        frozen[fault + "_id"] = str(uuid4())
    elif fault == "labs":
        frozen["laboratory_ids"] = []
    elif fault == "columns":
        frozen["columns"].reverse()
    elif fault == "time":
        frozen["snapshot_at"] = "2026-01-01T00:00:00Z"
    elif fault == "row_scope":
        frozen["rows"][0]["laboratory_id"] = str(uuid4())
    elif fault == "row_value":
        frozen["rows"][0]["explanation"] = {"private": "value"}
    elif fault == "extra":
        frozen["user_email"] = "private"
    value = replace(
        value,
        snapshot="{" if fault == "json" else canonical_json(frozen),
        format="pdf" if fault == "pdf" else "csv",
    )
    with pytest.raises(ReportInvalid):
        csv_bytes(value)


def test_report_csv_output_bound_and_cancellation(monkeypatch):
    monkeypatch.setattr("packages.domain.report_execution.MAX_REPORT_BYTES", 10)
    with pytest.raises(ReportInvalid):
        csv_bytes(source())
    with pytest.raises(LeaseLost):
        csv_bytes(source(), Mock(side_effect=LeaseLost()))


def test_report_key_uses_smallest_lab_and_artifact_requires_exact_version():
    value = source()
    frozen, filters = json.loads(value.snapshot), json.loads(value.filters)
    labs = sorted([*filters["laboratory_ids"], str(uuid4())])
    frozen["laboratory_ids"] = filters["laboratory_ids"] = labs
    value = replace(value, snapshot=canonical_json(frozen), filters=canonical_json(filters))
    payload = csv_bytes(value)
    result = artifact_for(value, payload, "pinned-version")
    assert (
        result.key == f"tenant/{value.tenant_id}/lab/{labs[0]}/reports/{value.export_id}/"
        f"{hashlib.sha256(payload).hexdigest()}.csv"
    )
    for bad in (
        replace(result, object_version="null"),
        replace(result, key=result.key + ".pdf"),
        replace(result, size_bytes=0),
        replace(result, object_version="bad\nversion"),
    ):
        with pytest.raises((ReportInvalid, ServiceError)):
            validate_artifact(value, bad)


def test_report_error_policy_matches_contract():
    contract = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/error-codes.json").read_text()
    )
    assert all(contract[code]["retryable"] == (code in REPORT_RETRYABLE) for code in REPORT_ERRORS)


@pytest.mark.parametrize("version", ["new-version", "null", ""])
def test_report_put_records_only_explicit_object_version(version):
    value = source()
    payload = csv_bytes(value)
    key = report_key(value, hashlib.sha256(payload).hexdigest())
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
                {"VersionId": version},
                {"Bucket": BUCKET, "Key": key, "Body": payload, "ContentType": REPORT_MIME},
            )
            if version == "new-version":
                assert storage.put_report(value, payload) == artifact_for(value, payload, version)
            else:
                with pytest.raises(ServiceError) as failure:
                    storage.put_report(value, payload)
                assert failure.value.code == "DEPENDENCY_UNAVAILABLE"
            stub.assert_no_pending_responses()
    finally:
        storage.close()


@pytest.mark.parametrize("conflict", [False, True])
def test_report_reuses_existing_version_only_after_hashing_its_bytes(conflict):
    value = source()
    payload = csv_bytes(value)
    key = report_key(value, hashlib.sha256(payload).hexdigest())
    stored = b"x" * len(payload) if conflict else payload
    storage = S3Storage(settings())
    stream = StreamingBody(io.BytesIO(stored), len(stored))
    metadata = {"VersionId": "existing", "ContentLength": len(stored), "ContentType": REPORT_MIME}
    try:
        with Stubber(storage.internal) as stub:
            stub.add_response("head_object", metadata, {"Bucket": BUCKET, "Key": key})
            stub.add_response(
                "get_object",
                {**metadata, "Body": stream},
                {"Bucket": BUCKET, "Key": key, "VersionId": "existing"},
            )
            if conflict:
                with pytest.raises(ServiceError) as failure:
                    storage.put_report(value, payload)
                assert failure.value.code == "INTERNAL_ERROR"
            else:
                assert storage.put_report(value, payload).object_version == "existing"
            stub.assert_no_pending_responses()
    finally:
        storage.close()
    assert stream._raw_stream.closed


def execution(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    service = ReportExecution(Mock())
    service.claim = Mock(return_value=SimpleNamespace(task_id="report", input=source()))
    service.heartbeat = Mock(return_value=True)
    service.commit = Mock()
    service.fail = Mock()
    return service


@pytest.mark.parametrize(
    "error,code",
    [
        (ReportInvalid(), "SCHEMA_MISMATCH"),
        (ServiceError("DEPENDENCY_UNAVAILABLE", 503, "private"), "DEPENDENCY_UNAVAILABLE"),
        (RuntimeError("private"), "INTERNAL_ERROR"),
    ],
)
def test_report_execution_classifies_failure(monkeypatch, error, code):
    service = execution(monkeypatch)
    with pytest.raises(type(error)):
        service.execute({}, Mock(side_effect=error))
    service.fail.assert_called_once_with(service.claim.return_value, code)
    service.commit.assert_not_called()


def test_report_execution_duplicate_does_not_prepare(monkeypatch):
    service = execution(monkeypatch)
    service.claim.return_value = None
    prepare = Mock()
    assert service.execute({}, prepare) is False
    prepare.assert_not_called()


def test_report_heartbeat_failure_prevents_failure_or_success_writes(monkeypatch):
    service = execution(monkeypatch)
    service.heartbeat.return_value = False
    monkeypatch.setattr(LeaseHeartbeat, "interval", 0.01)

    def prepare(source, cancelled):
        assert cancelled.wait(2)
        return artifact_for(source, csv_bytes(source), "orphan")

    with pytest.raises(LeaseLost):
        service.execute({}, prepare)
    service.commit.assert_not_called()
    service.fail.assert_not_called()


def stalled_report(*args):
    time.sleep(60)


@pytest.mark.parametrize("cancel", [False, True])
def test_report_child_deadline_and_cancellation_reap_process(monkeypatch, cancel):
    monkeypatch.setattr("apps.worker.report_export.prepare_child", stalled_report)
    monkeypatch.setattr("apps.worker.report_export.REPORT_SECONDS", 5 if cancel else 0.2)
    cancelled = Event()
    timer = Timer(0.1, cancelled.set) if cancel else None
    before = {p.pid for p in multiprocessing.active_children()}
    try:
        if timer:
            timer.start()
        with pytest.raises(LeaseLost if cancel else ServiceError) as error:
            BoundedReportPrepare(settings())(source(), cancelled)
        if not cancel:
            assert error.value.code == "STAGE_TIMEOUT"
    finally:
        if timer:
            timer.cancel()
            timer.join()
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_report_worker_is_opt_in_and_separate_queue(monkeypatch):
    from apps.worker.app.main import create_celery_app
    from apps.worker.dispatch import send_dispatch
    from apps.worker.run import main

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://labsafe.test")
    for name in ("IMAGE_VALIDATION", "INFERENCE", "RULE_EVALUATION", "REPORT_EXPORT"):
        monkeypatch.setenv(f"WORKER_{name}_ENABLED", "0")
    app = create_celery_app()
    try:
        assert "labsafe.tasks.dispatch" not in app.tasks
    finally:
        app.close()
    monkeypatch.setenv("WORKER_REPORT_EXPORT_ENABLED", "1")
    with (
        patch.object(S3Settings, "from_environment", return_value=settings()),
        patch("apps.worker.report_export.consume_report") as consume,
    ):
        app = create_celery_app()
        try:
            app.tasks["labsafe.tasks.dispatch"].run({"task_type": "report_export"})
            consume.assert_called_once()
        finally:
            app.close()
    fake = Mock()
    with patch("apps.worker.dispatch.create_celery_app", return_value=fake):
        send_dispatch(str(uuid4()), {"task_type": "report_export"})
    assert fake.send_task.call_args.kwargs["queue"] == "q.reports"
    with (
        patch("apps.worker.run.create_celery_app", return_value=fake),
        patch("sys.argv", ["worker"]),
    ):
        main()
    assert "--queues=q.reports" in fake.worker_main.call_args.args[0]
