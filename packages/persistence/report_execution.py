"""Report-first locking, execution fences and atomic CSV result registration."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from packages.domain.job_execution import retry_delay
from packages.domain.report_execution import (
    REPORT_ERRORS,
    REPORT_RETRYABLE,
    ReportInput,
    ReportLease,
    ReportLeaseLost,
    validate_artifact,
)
from packages.domain.uploads import identifier
from packages.persistence.dispatch import db_now, decoded, validate_dispatch
from packages.persistence.security import append_audit
from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json


def _task(connection, tenant, task_id, *, lock=False):
    return (
        connection.execute(
            text(
                "SELECT * FROM task_runs WHERE tenant_id=:tenant AND id=:id"
                + (" FOR UPDATE" if lock else "")
            ),
            {"tenant": tenant, "id": task_id},
        )
        .mappings()
        .first()
    )


def _load(connection, tenant, task_id):
    require_transaction(connection)
    locator = _task(connection, tenant, task_id)
    if locator is None or locator["task_type"] != "report_export":
        return None, None
    report = (
        connection.execute(
            text("SELECT * FROM report_exports WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
            {"tenant": tenant, "id": locator["resource_id"]},
        )
        .mappings()
        .first()
    )
    task = _task(connection, tenant, task_id, lock=True)
    if (
        report is None
        or task is None
        or task["task_type"] != "report_export"
        or task["resource_id"] != report["id"]
        or task["logical_key"] != f"report_export:{report['id']}"
        or decoded(task["payload"]) != {"export_id": report["id"]}
    ):
        return task, None
    return task, report


def _input(report):
    return ReportInput(
        report["id"],
        report["tenant_id"],
        report["requested_by"],
        report["format"],
        canonical_json(decoded(report["filters"])),
        timestamp(report["snapshot_at"]),
        canonical_json(decoded(report["snapshot"])),
        report["version"],
    )


def _owned(task, lease, now):
    return (
        task is not None
        and task["state"] == "leased"
        and task["lease_owner"] == lease.owner
        and task["fencing_token"] == lease.token
        and task["replay_generation"] == lease.generation
        and task["attempt"] == lease.attempt
        and task["lease_until"] is not None
        and task["lease_until"] > now
    )


def _current(report, lease):
    return report is not None and report["status"] == "running" and _input(report) == lease.input


def _attempt(connection, task, status, code, now):
    changed = connection.execute(
        text(
            "UPDATE task_attempts SET status=:status,error_code=:code,finished_at=:now,"
            "updated_at=:now,version=version+1 WHERE tenant_id=:tenant AND task_id=:id "
            "AND replay_generation=:generation AND attempt=:attempt AND fencing_token=:token "
            "AND lease_owner=:owner AND status='running'"
        ),
        {
            "status": status,
            "code": code,
            "now": now,
            "tenant": task["tenant_id"],
            "id": task["id"],
            "generation": task["replay_generation"],
            "attempt": task["attempt"],
            "token": task["fencing_token"],
            "owner": task["lease_owner"],
        },
    ).rowcount
    if changed != 1:
        raise RuntimeError("Missing current report attempt")


def _settle(connection, task, report, now, code, *, abandoned=False):
    retry = report is not None and code in REPORT_RETRYABLE and task["attempt"] < 4
    state = "retry_wait" if retry else ("dead_letter" if code in REPORT_RETRYABLE else "failed")
    _attempt(connection, task, "abandoned" if abandoned else "failed", code, now)
    connection.execute(
        text(
            "UPDATE task_runs SET state=:state,last_error_code=:code,lease_owner=NULL,"
            "lease_until=NULL,heartbeat_at=NULL,fencing_token=fencing_token+1,"
            "available_at=:available,finished_at=:finished,updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {
            "state": state,
            "code": code,
            "now": now,
            "tenant": task["tenant_id"],
            "id": task["id"],
            "available": now + retry_delay(task["attempt"]) if retry else now,
            "finished": None if retry else now,
        },
    )
    if report is not None:
        connection.execute(
            text(
                "UPDATE report_exports SET status=:status,last_error_code=:code,"
                "version=version+1,updated_at=:now WHERE tenant_id=:tenant AND id=:id"
            ),
            {
                "status": "queued" if retry else "failed",
                "code": code,
                "now": now,
                "tenant": task["tenant_id"],
                "id": report["id"],
            },
        )
    return state


def claim(connection, message, owner):
    validate_dispatch(message)
    identifier(owner)
    if message["task_type"] != "report_export":
        raise ValueError("Expected report dispatch")
    task, report = _load(connection, message["tenant_id"], message["task_id"])
    if task is None:
        return None
    for counter in ("replay_generation", "dispatch_sequence"):
        if message[counter] > task[counter]:
            raise ValueError("Future report dispatch")
        if message[counter] < task[counter]:
            return None
    if message["resource_id"] != task["resource_id"] or message["payload"] != decoded(
        task["payload"]
    ):
        raise ValueError("Report dispatch binding mismatch")
    now = db_now(connection)
    if (
        task["state"] not in {"ready", "retry_wait"}
        or task["available_at"] > now
        or task["attempt"] >= 4
        or report is None
        or report["status"] != "queued"
        or report["format"] != "csv"
    ):
        return None
    if any(
        report[name] is not None
        for name in ("object_key", "object_version", "checksum", "size_bytes", "expires_at")
    ):
        raise ValueError("Queued report already has an artifact")
    source = replace(_input(report), version=report["version"] + 1)
    attempt, token = task["attempt"] + 1, task["fencing_token"] + 1
    params = {
        "tenant": task["tenant_id"],
        "id": task["id"],
        "attempt": attempt,
        "token": token,
        "owner": owner,
        "now": now,
        "until": now + timedelta(seconds=60),
    }
    connection.execute(
        text(
            "UPDATE task_runs SET state='leased',attempt=:attempt,fencing_token=:token,"
            "lease_owner=:owner,lease_until=:until,heartbeat_at=:now,started_at=COALESCE(started_at,:now),"
            "finished_at=NULL,last_error_code=NULL,updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        params,
    )
    connection.execute(
        text(
            "INSERT INTO task_attempts (id,tenant_id,task_id,replay_generation,attempt,"
            "fencing_token,lease_owner,started_at,status,created_at,updated_at) VALUES "
            "(:attempt_id,:tenant,:id,:generation,:attempt,:token,:owner,:now,'running',:now,:now)"
        ),
        {**params, "attempt_id": str(uuid4()), "generation": task["replay_generation"]},
    )
    connection.execute(
        text(
            "UPDATE report_exports SET status='running',last_error_code=NULL,"
            "version=version+1,updated_at=:now WHERE tenant_id=:tenant AND id=:id"
        ),
        {"tenant": task["tenant_id"], "id": report["id"], "now": now},
    )
    return ReportLease(
        task["tenant_id"], task["id"], owner, token, task["replay_generation"], attempt, source
    )


def heartbeat(connection, lease):
    task, report = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now) or not _current(report, lease):
        return False
    connection.execute(
        text(
            "UPDATE task_runs SET lease_until=:until,heartbeat_at=:now,updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {
            "until": now + timedelta(seconds=60),
            "now": now,
            "tenant": lease.tenant_id,
            "id": lease.task_id,
        },
    )
    return True


def fail(connection, lease, code):
    if code not in REPORT_ERRORS:
        raise ValueError("Unknown report error")
    task, report = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now) or not _current(report, lease):
        return False
    _settle(connection, task, report, now, code)
    return True


def commit_result(connection, lease, artifact):
    validate_artifact(lease.input, artifact)
    task, report = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now) or not _current(report, lease):
        raise ReportLeaseLost()
    connection.execute(
        text(
            "UPDATE report_exports SET status='ready',object_key=:key,checksum=:checksum,"
            "object_version=:object_version,size_bytes=:size,expires_at=:expiry,"
            "last_error_code=NULL,version=version+1,updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {
            "key": artifact.key,
            "checksum": artifact.checksum,
            "object_version": artifact.object_version,
            "size": artifact.size_bytes,
            "expiry": now + timedelta(hours=24),
            "now": now,
            "tenant": lease.tenant_id,
            "id": report["id"],
        },
    )
    _attempt(connection, task, "succeeded", None, now)
    append_audit(
        connection,
        principal=SimpleNamespace(tenant_id=lease.tenant_id, user_id=None),
        action="report.export_ready",
        resource_type="report_export",
        resource_id=report["id"],
        changes={
            "status": "ready",
            "checksum": artifact.checksum,
            "size_bytes": artifact.size_bytes,
        },
        reason="Generate CSV from fixed report snapshot",
        request_id=lease.task_id,
    )
    connection.execute(
        text(
            "UPDATE task_runs SET state='succeeded',lease_owner=NULL,lease_until=NULL,"
            "heartbeat_at=NULL,last_error_code=NULL,finished_at=:now,"
            "updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {"now": now, "tenant": lease.tenant_id, "id": lease.task_id},
    )
    return True


def expired_candidates(connection, limit=100):
    require_transaction(connection)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Batch limit must be 1..100")
    return [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT tenant_id,id,fencing_token,replay_generation FROM task_runs "
                "WHERE task_type='report_export' AND state='leased' "
                "AND lease_until<=UTC_TIMESTAMP(3) "
                "ORDER BY lease_until,id LIMIT :limit"
            ),
            {"limit": limit},
        ).mappings()
    ]


def recover(connection, candidate):
    task, report = _load(connection, candidate["tenant_id"], candidate["id"])
    now = db_now(connection)
    if (
        task is None
        or task["state"] != "leased"
        or task["lease_until"] is None
        or task["lease_until"] > now
        or task["fencing_token"] != candidate["fencing_token"]
        or task["replay_generation"] != candidate["replay_generation"]
    ):
        return False
    if report is None or report["status"] != "running" or report["format"] != "csv":
        _settle(connection, task, None, now, "LEASE_LOST", abandoned=True)
    else:
        _settle(connection, task, report, now, "STAGE_TIMEOUT", abandoned=True)
    return True
