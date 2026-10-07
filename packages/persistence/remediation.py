"""Transactional remediation tasks and evidence."""

# SQL projections are kept aligned with the public projection contract.
# ruff: noqa: E501

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text

from packages.domain.security import Permission, ServiceError, authorize, not_found
from packages.domain.workflow import (
    Evidence,
    Finding,
    Image,
    RemediationDecision,
    Task,
    accept_task,
    cannot_remediate,
    dispatch_finding,
    reassign_task,
    recheck_task,
    reject_task,
    submit_evidence,
)
from packages.persistence.dispatch import db_now
from packages.persistence.foundations import lab_filter, page_query, project
from packages.persistence.security import append_audit, load_principal
from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json


def _row(connection, tenant, task_id, *, lock=False):
    return (
        connection.execute(
            text(
                "SELECT t.id,t.created_at,t.updated_at,t.version,t.tenant_id,t.finding_id,"
                "t.laboratory_id,t.assignee_id,t.status,t.priority,t.due_at,t.description,"
                "t.latest_evidence_id,t.closed_at,f.version AS finding_version,f.status AS finding_status,"
                "f.item_id,f.run_id,f.rule_evaluation_id,f.superseded_at "
                "FROM remediation_tasks t JOIN findings f ON f.tenant_id=t.tenant_id AND f.id=t.finding_id "
                "WHERE t.tenant_id=:tenant AND t.id=:id" + (" FOR UPDATE" if lock else "")
            ),
            {"tenant": tenant, "id": task_id},
        )
        .mappings()
        .first()
    )


def _finding_row(connection, tenant, finding_id, *, lock=False):
    return (
        connection.execute(
            text(
                "SELECT f.id,f.tenant_id,r.laboratory_id,f.version,f.status,f.item_id,f.run_id,"
                "f.rule_evaluation_id,f.superseded_at FROM findings f "
                "JOIN inference_runs r ON r.tenant_id=f.tenant_id AND r.item_id=f.item_id "
                "AND r.id=f.run_id WHERE f.tenant_id=:tenant AND f.id=:id"
                + (" FOR UPDATE" if lock else "")
            ),
            {"tenant": tenant, "id": finding_id},
        )
        .mappings()
        .first()
    )


def _principal(connection, tenant, user_id):
    return load_principal(connection, tenant, user_id)


def _task_domain(row):
    return Task(
        id=row["id"],
        tenant_id=row["tenant_id"],
        laboratory_id=row["laboratory_id"],
        version=row["version"],
        status=row["status"],
        finding_id=row["finding_id"],
        assignee_id=row["assignee_id"],
        latest_evidence_id=row["latest_evidence_id"],
    )


def _finding_domain(row):
    return Finding(
        id=row.get("finding_id", row["id"]),
        tenant_id=row["tenant_id"],
        laboratory_id=row["laboratory_id"],
        version=row["finding_version"],
        status=row["finding_status"],
        item_id=row["item_id"],
        run_id=row["run_id"],
        evaluation_id=row["rule_evaluation_id"],
        superseded=row["superseded_at"] is not None,
        evidence_ready=bool(row.get("latest_evidence_id")),
    )


def _project(row):
    value = project(dict(row))
    for key in ("due_at", "closed_at"):
        value[key] = timestamp(row[key]) if row[key] else None
    value["is_overdue"] = (
        row["status"] not in {"closed", "cannot_remediate"} and row["due_at"] <= datetime.utcnow()
    )
    value["allowed_actions"] = []
    value.pop("tenant_id", None)
    value.pop("finding_version", None)
    value.pop("finding_status", None)
    value.pop("item_id", None)
    value.pop("run_id", None)
    value.pop("rule_evaluation_id", None)
    value.pop("superseded_at", None)
    return value


def _event(connection, tenant, event_type, task_id, version, payload, now):
    event_id = str(uuid4())
    envelope = {
        "schema_version": "1.1",
        "tenant_id": tenant,
        "trace_id": uuid4().hex,
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": "remediation_task",
        "aggregate_id": task_id,
        "occurred_at": timestamp(now),
        "aggregate_version": version,
        "payload": payload,
    }
    connection.execute(
        text(
            "INSERT INTO outbox_events (id,tenant_id,event_type,aggregate_type,aggregate_id,"
            "aggregate_version,payload,state,available_at,created_at,updated_at) VALUES "
            "(:id,:tenant,:type,'remediation_task',:task,:version,:payload,'pending',:now,:now,:now)"
        ),
        {
            "id": event_id,
            "tenant": tenant,
            "type": event_type,
            "task": task_id,
            "version": version,
            "payload": canonical_json(envelope),
            "now": now,
        },
    )
    return event_id


def _notify(connection, tenant, event_id, recipient, resource_id, kind):
    connection.execute(
        text(
            "INSERT INTO notifications (id,tenant_id,event_id,recipient_id,type,resource_type,resource_id) "
            "VALUES (:id,:tenant,:event,:recipient,:type,'remediation_task',:resource)"
        ),
        {
            "id": str(uuid4()),
            "tenant": tenant,
            "event": event_id,
            "recipient": recipient,
            "type": kind,
            "resource": resource_id,
        },
    )


def _commit_status(
    connection, actor, row, decision: RemediationDecision, reason, request_id, action
):
    now = db_now(connection)
    task_version = row["version"] + 1
    changed = connection.execute(
        text(
            "UPDATE remediation_tasks SET status=:status,version=:version,"
            "closed_at=:closed,updated_at=:now WHERE tenant_id=:tenant AND id=:id AND version=:old"
        ),
        {
            "status": str(decision.task_status),
            "version": task_version,
            "closed": now if str(decision.task_status) == "closed" else None,
            "now": now,
            "tenant": actor.tenant_id,
            "id": row["id"],
            "old": row["version"],
        },
    ).rowcount
    if changed != 1:
        raise ServiceError("VERSION_CONFLICT", 409, "Remediation task changed during command")
    if decision.finding_status != row["finding_status"]:
        changed = connection.execute(
            text(
                "UPDATE findings SET status=:status,version=version+1,updated_at=:now "
                "WHERE tenant_id=:tenant AND id=:id AND version=:version"
            ),
            {
                "status": str(decision.finding_status),
                "now": now,
                "tenant": actor.tenant_id,
                "id": row["finding_id"],
                "version": row["finding_version"],
            },
        ).rowcount
        if changed != 1:
            raise ServiceError("VERSION_CONFLICT", 409, "Finding changed during command")
    event_type = {
        "closed": "RemediationClosed",
        "pending_recheck": "EvidenceSubmitted",
        "pending_dispatch": "RemediationDispatched",
    }.get(str(decision.task_status))
    event_id = (
        _event(
            connection,
            actor.tenant_id,
            event_type,
            row["id"],
            task_version,
            {"finding_id": row["finding_id"], "actor_id": actor.user_id},
            now,
        )
        if event_type
        else None
    )
    if event_id and str(decision.task_status) == "pending_dispatch":
        _notify(
            connection,
            actor.tenant_id,
            event_id,
            row["assignee_id"],
            row["id"],
            "remediation_assigned",
        )
    connection.execute(
        text(
            "INSERT INTO review_actions (id,tenant_id,actor_id,resource_type,resource_id,action,reason,before_version,after_version) "
            "VALUES (:id,:tenant,:actor,'remediation_task',:resource,:action,:reason,:before,:after)"
        ),
        {
            "id": str(uuid4()),
            "tenant": actor.tenant_id,
            "actor": actor.user_id,
            "resource": row["id"],
            "action": action,
            "reason": reason,
            "before": row["version"],
            "after": task_version,
        },
    )
    append_audit(
        connection,
        principal=actor,
        action=action,
        resource_type="remediation_task",
        resource_id=row["id"],
        changes={"status": str(decision.task_status), "version": task_version},
        reason=reason,
        request_id=request_id,
    )
    return _project(_row(connection, actor.tenant_id, row["id"]))


def get_task(connection, actor, task_id):
    row = _row(connection, actor.tenant_id, task_id)
    if row is None:
        raise not_found()
    authorize(actor, Permission.READ, row["laboratory_id"])
    return _project(row)


def authorize_finding(connection, actor, finding_id):
    row = _finding_row(connection, actor.tenant_id, finding_id)
    if row is None:
        raise not_found()
    authorize(actor, Permission.DISPATCH, row["laboratory_id"])
    return row


def list_tasks(connection, actor, page, page_size, laboratory_id=None, status=None, overdue=None):
    params = {"tenant": actor.tenant_id}
    where = "t.tenant_id=:tenant" + lab_filter(actor, "t.laboratory_id", params, laboratory_id)
    if status:
        where += " AND t.status=:status"
        params["status"] = status
    if overdue is True:
        where += " AND t.status NOT IN ('closed','cannot_remediate') AND t.due_at<UTC_TIMESTAMP(3)"
    elif overdue is False:
        where += " AND (t.status IN ('closed','cannot_remediate') OR t.due_at>=UTC_TIMESTAMP(3))"
    return page_query(
        connection,
        "remediation_tasks t JOIN findings f ON f.tenant_id=t.tenant_id AND f.id=t.finding_id",
        "t.id,t.created_at,t.updated_at,t.version,t.tenant_id,t.finding_id,t.laboratory_id,t.assignee_id,t.status,t.priority,t.due_at,t.description,t.latest_evidence_id,t.closed_at,f.version AS finding_version,f.status AS finding_status,f.item_id,f.run_id,f.rule_evaluation_id,f.superseded_at",
        where,
        params,
        page,
        page_size,
        project_row=_project,
        order_by="t.created_at DESC,t.id DESC",
    )


def list_evidence(connection, actor, task_id, page, page_size, laboratory_id=None):
    row = _row(connection, actor.tenant_id, task_id)
    if row is None:
        raise not_found()
    authorize(actor, Permission.READ, row["laboratory_id"])
    if laboratory_id is not None and laboratory_id != row["laboratory_id"]:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    params = {"tenant": actor.tenant_id, "task": task_id}
    return page_query(
        connection,
        "remediation_evidence e",
        "e.id,e.created_at,e.updated_at,e.version,e.task_id,e.description,e.submitted_by",
        "e.tenant_id=:tenant AND e.task_id=:task",
        params,
        page,
        page_size,
        project_row=lambda value: _evidence_projection(connection, actor.tenant_id, value),
        order_by="e.created_at ASC,e.id ASC",
    )


def _evidence_projection(connection, tenant, row):
    value = project(dict(row))
    value["image_ids"] = list(
        connection.execute(
            text(
                "SELECT image_id FROM evidence_images WHERE tenant_id=:tenant AND evidence_id=:id ORDER BY ordinal,image_id"
            ),
            {"tenant": tenant, "id": row["id"]},
        ).scalars()
    )
    return value


def dispatch(connection, actor, finding_id, body, request_id):
    require_transaction(connection)
    finding = _finding_row(connection, actor.tenant_id, finding_id, lock=True)
    if finding is None:
        raise not_found()
    authorize(actor, Permission.DISPATCH, finding["laboratory_id"])
    active = bool(
        connection.scalar(
            text("SELECT status='active' FROM users WHERE tenant_id=:tenant AND id=:id"),
            {"tenant": actor.tenant_id, "id": body["assignee_id"]},
        )
    )
    if not active:
        raise ServiceError("VALIDATION_ERROR", 422, "Assignee must be active")
    assignee = _principal(connection, actor.tenant_id, body["assignee_id"])
    now = datetime.now(timezone.utc)
    decision = dispatch_finding(
        actor,
        _finding_domain(
            {
                **finding,
                "finding_version": finding["version"],
                "finding_status": finding["status"],
            }
        ),
        expected_version=body["expected_version"],
        assignee=assignee,
        assignee_active=active,
        due_at=body["due_at"],
        now=now,
    )
    task_id = str(uuid4())
    changed = connection.execute(
        text(
            "INSERT INTO remediation_tasks (id,tenant_id,finding_id,laboratory_id,assignee_id,status,priority,due_at,description) VALUES (:id,:tenant,:finding,:lab,:assignee,:status,:priority,:due,:description)"
        ),
        {
            "id": task_id,
            "tenant": actor.tenant_id,
            "finding": finding_id,
            "lab": finding["laboratory_id"],
            "assignee": body["assignee_id"],
            "status": str(decision.task_status),
            "priority": body["priority"],
            "due": body["due_at"].replace(tzinfo=None),
            "description": body["description"],
        },
    ).rowcount
    if changed != 1:
        raise ServiceError("VERSION_CONFLICT", 409, "Finding changed during dispatch")
    changed = connection.execute(
        text(
            "UPDATE findings SET status=:status,version=version+1,updated_at=:now WHERE tenant_id=:tenant AND id=:id AND version=:version"
        ),
        {
            "status": str(decision.finding_status),
            "now": now,
            "tenant": actor.tenant_id,
            "id": finding_id,
            "version": finding["version"],
        },
    ).rowcount
    if changed != 1:
        raise ServiceError("VERSION_CONFLICT", 409, "Finding changed during dispatch")
    event_id = _event(
        connection,
        actor.tenant_id,
        "RemediationDispatched",
        task_id,
        1,
        {"finding_id": finding_id, "assignee_id": body["assignee_id"]},
        now,
    )
    _notify(
        connection, actor.tenant_id, event_id, body["assignee_id"], task_id, "remediation_assigned"
    )
    append_audit(
        connection,
        principal=actor,
        action="remediation.dispatch",
        resource_type="remediation_task",
        resource_id=task_id,
        changes={
            "finding_id": finding_id,
            "assignee_id": body["assignee_id"],
            "status": str(decision.task_status),
        },
        reason=body["description"],
        request_id=request_id,
    )
    return _project(_row(connection, actor.tenant_id, task_id))


def command(connection, actor, task_id, body, operation, request_id):
    require_transaction(connection)
    row = _row(connection, actor.tenant_id, task_id, lock=True)
    if row is None:
        raise not_found()
    finding = _finding_domain(row)
    task = _task_domain(row)
    expected = body["expected_version"]
    if operation == "accept":
        decision = accept_task(actor, task, finding, expected_version=expected)
        reason = "Task accepted"
    elif operation == "cannot-remediate":
        decision = cannot_remediate(
            actor, task, finding, expected_version=expected, reason=body["reason"]
        )
        reason = body["reason"]
    elif operation == "reassign":
        assignee = _principal(connection, actor.tenant_id, body["assignee_id"])
        active = bool(
            connection.scalar(
                text("SELECT status='active' FROM users WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": actor.tenant_id, "id": body["assignee_id"]},
            )
        )
        decision = reassign_task(
            actor,
            task,
            finding,
            expected_version=expected,
            assignee=assignee,
            assignee_active=active,
            due_at=body["due_at"],
            now=datetime.now(timezone.utc),
            reason=body["reason"],
        )
        changed = connection.execute(
            text(
                "UPDATE remediation_tasks SET assignee_id=:assignee,due_at=:due WHERE tenant_id=:tenant AND id=:id AND version=:version"
            ),
            {
                "assignee": body["assignee_id"],
                "due": body["due_at"].replace(tzinfo=None),
                "tenant": actor.tenant_id,
                "id": task_id,
                "version": row["version"],
            },
        ).rowcount
        if changed != 1:
            raise ServiceError("VERSION_CONFLICT", 409, "Remediation task changed during command")
        row = {**row, "assignee_id": body["assignee_id"]}
        reason = body["reason"]
    elif operation in {"recheck", "reject"}:
        evidence = (
            connection.execute(
                text(
                    "SELECT id,tenant_id,task_id,submitted_by FROM remediation_evidence WHERE tenant_id=:tenant AND id=:id AND task_id=:task"
                ),
                {"tenant": actor.tenant_id, "id": body["evidence_id"], "task": task_id},
            )
            .mappings()
            .first()
        )
        if evidence is None:
            raise not_found()
        if actor.user_id in {row["assignee_id"], evidence["submitted_by"]}:
            raise ServiceError("FORBIDDEN", 403, "Reviewer cannot be assignee or submitter")
        ev = Evidence(
            id=evidence["id"],
            tenant_id=evidence["tenant_id"],
            task_id=evidence["task_id"],
            submitted_by=evidence["submitted_by"],
        )
        decision = (
            recheck_task(
                actor,
                task,
                finding,
                expected_version=expected,
                evidence=ev,
                evidence_id=body["evidence_id"],
                reason=body["reason"],
            )
            if operation == "recheck"
            else reject_task(
                actor,
                task,
                finding,
                expected_version=expected,
                evidence=ev,
                evidence_id=body["evidence_id"],
                reason=body["reason"],
            )
        )
        reason = body["reason"]
    else:
        raise ServiceError("VALIDATION_ERROR", 422, "Unknown remediation command")
    return _commit_status(
        connection, actor, row, decision, reason, request_id, f"remediation.{operation}"
    )


def submit(connection, actor, task_id, body, request_id):
    require_transaction(connection)
    row = _row(connection, actor.tenant_id, task_id, lock=True)
    if row is None:
        raise not_found()
    images = []
    for image_id in body["image_ids"]:
        image = (
            connection.execute(
                text(
                    "SELECT id,tenant_id,laboratory_id,remediation_task_id,status FROM asset_images WHERE tenant_id=:tenant AND id=:id AND remediation_task_id=:task FOR UPDATE"
                ),
                {"tenant": actor.tenant_id, "id": image_id, "task": task_id},
            )
            .mappings()
            .first()
        )
        if image is None:
            raise not_found()
        images.append(
            Image(
                id=image["id"],
                tenant_id=image["tenant_id"],
                laboratory_id=image["laboratory_id"],
                owner_type="remediation_task",
                owner_id=task_id,
                location_id=None,
                status=image["status"],
                available=image["status"] == "ready",
            )
        )
    decision = submit_evidence(
        actor,
        _task_domain(row),
        _finding_domain(row),
        expected_version=body["expected_version"],
        images=tuple(images),
        description=body["description"],
    )
    evidence_id = str(uuid4())
    connection.execute(
        text(
            "INSERT INTO remediation_evidence (id,tenant_id,task_id,description,submitted_by) VALUES (:id,:tenant,:task,:description,:user)"
        ),
        {
            "id": evidence_id,
            "tenant": actor.tenant_id,
            "task": task_id,
            "description": body["description"],
            "user": actor.user_id,
        },
    )
    for ordinal, image_id in enumerate(body["image_ids"]):
        connection.execute(
            text(
                "INSERT INTO evidence_images (id,tenant_id,task_id,evidence_id,image_id,ordinal) VALUES (:id,:tenant,:task,:evidence,:image,:ordinal)"
            ),
            {
                "id": str(uuid4()),
                "tenant": actor.tenant_id,
                "task": task_id,
                "evidence": evidence_id,
                "image": image_id,
                "ordinal": ordinal,
            },
        )
    connection.execute(
        text(
            "UPDATE remediation_tasks SET latest_evidence_id=:evidence WHERE tenant_id=:tenant AND id=:id"
        ),
        {"evidence": evidence_id, "tenant": actor.tenant_id, "id": task_id},
    )
    row = _row(connection, actor.tenant_id, task_id, lock=True)
    _commit_status(
        connection,
        actor,
        row,
        decision,
        body["description"],
        request_id,
        "remediation.submit-evidence",
    )
    return _evidence_projection(
        connection,
        actor.tenant_id,
        connection.execute(
            text("SELECT * FROM remediation_evidence WHERE tenant_id=:tenant AND id=:id"),
            {"tenant": actor.tenant_id, "id": evidence_id},
        )
        .mappings()
        .one(),
    )
