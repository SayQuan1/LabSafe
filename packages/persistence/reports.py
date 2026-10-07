"""Freeze authorized report fields and enqueue work in a caller-owned transaction."""

from uuid import UUID, uuid4

from sqlalchemy import text

from packages.domain.reports import COLUMNS, MAX_FINDINGS, validate_request
from packages.domain.security import Permission, ServiceError, authorize, not_found
from packages.persistence.dispatch import db_now, decoded, validate_dispatch
from packages.persistence.security import append_audit
from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json

# Every join is tenant-scoped; only the current run/revision/evaluation contributes findings.
SOURCE = (
    "inspection_items ii JOIN inspections i ON i.tenant_id=ii.tenant_id "
    "AND i.id=ii.inspection_id AND i.laboratory_id=ii.laboratory_id "
    "LEFT JOIN inference_runs r ON r.tenant_id=ii.tenant_id "
    "AND r.item_id=ii.id AND r.id=ii.current_run_id "
    "LEFT JOIN findings f ON f.tenant_id=ii.tenant_id AND f.item_id=ii.id "
    "AND f.run_id=ii.current_run_id AND f.fact_revision_id=ii.current_fact_revision_id "
    "AND f.rule_evaluation_id=ii.current_evaluation_id AND f.superseded_at IS NULL "
    "LEFT JOIN remediation_tasks rt ON rt.tenant_id=f.tenant_id AND rt.finding_id=f.id"
)


def authorize_labs(connection, actor, labs, permission):
    if not labs:
        raise ServiceError("STATE_CONFLICT", 409, "Report scope is missing")
    params = {"tenant": actor.tenant_id, **{f"lab{i}": lab for i, lab in enumerate(labs)}}
    names = ",".join(f":lab{i}" for i in range(len(labs)))
    present = (
        connection.execute(
            text(f"SELECT id FROM laboratories WHERE tenant_id=:tenant AND id IN ({names})"),
            params,
        )
        .scalars()
        .all()
    )
    if set(present) != set(labs):
        raise not_found()
    for lab in labs:
        authorize(actor, permission, lab)


def _source(filters, tenant, start, end):
    params = {
        "tenant": tenant,
        "start": start,
        "end": end,
        **{f"lab{i}": lab for i, lab in enumerate(filters["laboratory_ids"])},
    }
    labs = ",".join(f":lab{i}" for i in range(len(filters["laboratory_ids"])))
    where = f"ii.tenant_id=:tenant AND ii.laboratory_id IN ({labs})"
    # Dates refer to inspection creation, inclusive in UTC. Zero-finding items remain rows.
    where += " AND i.created_at>=:start AND i.created_at<=:end"
    for name, column in (("severity", "severity"), ("finding_status", "status")):
        values = filters[name]
        if values:
            binds = ",".join(f":{name}{i}" for i in range(len(values)))
            params.update({f"{name}{i}": value for i, value in enumerate(values)})
            where += f" AND (f.{column} IN ({binds}) OR f.id IS NULL)"
    return where, params


def _snapshot_rows(connection, filters, tenant, start, end, now):
    where, params = _source(filters, tenant, start, end)
    count = connection.scalar(text(f"SELECT COUNT(f.id) FROM {SOURCE} WHERE {where}"), params)
    if count > MAX_FINDINGS:
        raise ServiceError(
            "VALIDATION_ERROR", 422, "Export exceeds 10000 findings; narrow the range"
        )
    rows = connection.execute(
        text(
            "SELECT i.id AS inspection_id,ii.id AS item_id,ii.laboratory_id,ii.location_id,"
            "ii.current_run_id AS run_id,ii.current_fact_revision_id AS fact_revision_id,"
            "r.rule_bundle_id,f.id AS finding_id,f.rule_id,f.severity,f.status,f.explanation,"
            f"rt.status AS task_status,ii.review_outcome FROM {SOURCE} WHERE {where} "
            "ORDER BY ii.laboratory_id,i.id,ii.id,f.id"
        ),
        params,
    ).mappings()
    return [{**dict(row), "snapshot_at": timestamp(now)} for row in rows], count


def get(connection, actor, export_id, *, permission=Permission.READ, current=False):
    require_transaction(connection)
    row = (
        connection.execute(
            text(
                "SELECT e.id,e.created_at,e.updated_at,e.version,e.format,e.status,e.snapshot_at,"
                "e.filters,e.expires_at,e.last_error_code,t.id AS job_id FROM report_exports e "
                "JOIN task_runs t ON t.tenant_id=e.tenant_id AND t.task_type='report_export' "
                "AND t.resource_id=e.id AND t.logical_key=CONCAT('report_export:',e.id) "
                "WHERE e.tenant_id=:tenant AND e.id=:id" + (" FOR SHARE" if current else "")
            ),
            {"tenant": actor.tenant_id, "id": export_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    authorize_labs(connection, actor, decoded(row["filters"])["laboratory_ids"], permission)
    return {
        "id": row["id"],
        "created_at": timestamp(row["created_at"]),
        "updated_at": timestamp(row["updated_at"]),
        "version": row["version"],
        "format": row["format"],
        "status": row["status"],
        "snapshot_at": timestamp(row["snapshot_at"]),
        "expires_at": timestamp(row["expires_at"]) if row["expires_at"] else None,
        "job_id": row["job_id"],
        "error_code": row["last_error_code"],
    }


def download_row(connection, actor, export_id):
    """Load private download evidence under the current tenant transaction."""
    require_transaction(connection)
    return (
        connection.execute(
            text(
                "SELECT id,tenant_id,requested_by,format,status,filters,expires_at,"
                "object_key,checksum,object_version,size_bytes FROM report_exports "
                "WHERE tenant_id=:tenant AND id=:id FOR SHARE"
            ),
            {"tenant": actor.tenant_id, "id": export_id},
        )
        .mappings()
        .first()
    )


def _outbox(connection, tenant, kind, aggregate, resource, payload, now):
    connection.execute(
        text(
            "INSERT INTO outbox_events (id,tenant_id,event_type,aggregate_type,aggregate_id,"
            "aggregate_version,payload,state,available_at,created_at,updated_at) "
            "VALUES (:id,:tenant,:kind,:aggregate,:resource,1,:payload,'pending',:now,:now,:now)"
        ),
        {
            "id": payload.get("event_id", str(uuid4())),
            "tenant": tenant,
            "kind": kind,
            "aggregate": aggregate,
            "resource": resource,
            "payload": canonical_json(payload),
            "now": now,
        },
    )


def create(connection, actor, body, request_id):
    require_transaction(connection)
    filters, start, end = validate_request(body)
    authorize_labs(connection, actor, filters["laboratory_ids"], Permission.EXPORT)
    now = db_now(connection)
    rows, count = _snapshot_rows(connection, filters, actor.tenant_id, start, end, now)
    export_id, task_id = str(uuid4()), str(uuid4())
    snapshot = {
        "schema_version": "1",
        "export_id": export_id,
        "tenant_id": actor.tenant_id,
        "laboratory_ids": filters["laboratory_ids"],
        "snapshot_at": timestamp(now),
        "columns": list(COLUMNS),
        "rows": rows,
    }
    connection.execute(
        text(
            "INSERT INTO report_exports (id,tenant_id,requested_by,format,filters,snapshot_at,"
            "snapshot,status,created_at,updated_at) "
            "VALUES (:id,:tenant,:user,:format,:filters,:now,:snapshot,'queued',:now,:now)"
        ),
        {
            "id": export_id,
            "tenant": actor.tenant_id,
            "user": actor.user_id,
            "format": body["format"],
            "filters": canonical_json(filters),
            "now": now,
            "snapshot": canonical_json(snapshot),
        },
    )
    payload = {"export_id": export_id}
    connection.execute(
        text(
            "INSERT INTO task_runs (id,tenant_id,task_type,resource_id,logical_key,payload,"
            "state,available_at,last_dispatched_at,created_at,updated_at) "
            "VALUES (:id,:tenant,'report_export',:resource,:key,:payload,"
            "'ready',:now,:now,:now,:now)"
        ),
        {
            "id": task_id,
            "tenant": actor.tenant_id,
            "resource": export_id,
            "key": f"report_export:{export_id}",
            "payload": canonical_json(payload),
            "now": now,
        },
    )
    dispatch = validate_dispatch(
        {
            "schema_version": "1.1",
            "tenant_id": actor.tenant_id,
            "trace_id": UUID(request_id).hex,
            "task_id": task_id,
            "task_type": "report_export",
            "resource_id": export_id,
            "replay_generation": 0,
            "dispatch_sequence": 1,
            "created_at": timestamp(now),
            "payload": payload,
        }
    )
    _outbox(connection, actor.tenant_id, "TaskDispatch", "task_run", task_id, dispatch, now)
    envelope = {
        "schema_version": "1.1",
        "tenant_id": actor.tenant_id,
        "trace_id": UUID(request_id).hex,
        "event_id": str(uuid4()),
        "event_type": "ReportRequested",
        "aggregate_type": "report_export",
        "aggregate_id": export_id,
        "occurred_at": timestamp(now),
        "aggregate_version": 1,
        "payload": payload,
    }
    _outbox(
        connection, actor.tenant_id, "ReportRequested", "report_export", export_id, envelope, now
    )
    append_audit(
        connection,
        principal=actor,
        action="report.export_requested",
        resource_type="report_export",
        resource_id=export_id,
        changes={"format": body["format"], "finding_count": count},
        reason="Report export requested",
        request_id=request_id,
    )
    return get(connection, actor, export_id)


def replay_source(connection, actor, locator):
    """Lock the report before its task; reject missing or mismatched immutable input."""
    report = (
        connection.execute(
            text("SELECT * FROM report_exports WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
            {"tenant": actor.tenant_id, "id": locator["resource_id"]},
        )
        .mappings()
        .first()
    )
    task = (
        connection.execute(
            text("SELECT * FROM task_runs WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
            {"tenant": actor.tenant_id, "id": locator["id"]},
        )
        .mappings()
        .first()
    )
    if report is None or task is None:
        return task, None
    filters = decoded(report["filters"])
    authorize_labs(connection, actor, filters["laboratory_ids"], Permission.EXPORT)
    snapshot = decoded(report["snapshot"])
    valid = (
        report["status"] == "failed"
        and task["task_type"] == "report_export"
        and task["resource_id"] == report["id"]
        and task["logical_key"] == f"report_export:{report['id']}"
        and decoded(task["payload"]) == {"export_id": report["id"]}
        and isinstance(snapshot, dict)
        and snapshot.get("schema_version") == "1"
        and snapshot.get("export_id") == report["id"]
        and snapshot.get("tenant_id") == actor.tenant_id
        and snapshot.get("laboratory_ids") == filters["laboratory_ids"]
        and snapshot.get("columns") == list(COLUMNS)
        and isinstance(snapshot.get("rows"), list)
        and report["version"] < 2147483647
    )
    return task, report if valid else None


def restore_failed(connection, actor, export_id, now):
    changed = connection.execute(
        text(
            "UPDATE report_exports SET status='queued',last_error_code=NULL,"
            "version=version+1,updated_at=:now WHERE tenant_id=:tenant AND id=:id "
            "AND status='failed'"
        ),
        {"tenant": actor.tenant_id, "id": export_id, "now": now},
    ).rowcount
    if changed != 1:
        raise ServiceError("STATE_CONFLICT", 409, "Report export is not replayable")
