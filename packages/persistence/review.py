"""Facts, findings, and review-decision persistence.

Commands lock the inspection item first, then its current run/evaluation and
the finding rows.  Every current-pointer change is fenced by the item's
expected version and emits an audit record in the same transaction.
"""

import json
from datetime import date
from uuid import uuid4

from sqlalchemy import text

from packages.domain.security import Permission, ServiceError, authorize, not_found, require_version
from packages.domain.workflow import (
    Finding,
    cannot_determine_finding,
    confirm_finding,
    reject_finding,
)
from packages.persistence.dispatch import db_now
from packages.persistence.foundations import lab_filter, page_query, project
from packages.persistence.security import append_audit
from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json


def _decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def _domain_event(
    connection, *, tenant, event_type, aggregate_type, aggregate_id, version, payload, now
):
    event_id = str(uuid4())
    event = {
        "schema_version": "1.1",
        "tenant_id": tenant,
        "trace_id": uuid4().hex,
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": timestamp(now),
        "aggregate_version": version,
        "payload": payload,
    }
    connection.execute(
        text(
            "INSERT INTO outbox_events (id,tenant_id,event_type,aggregate_type,aggregate_id,"
            "aggregate_version,payload,state,available_at,created_at,updated_at) VALUES "
            "(:id,:tenant,:event_type,:aggregate_type,:aggregate_id,:version,:payload,'pending',"
            ":now,:now,:now)"
        ),
        {
            "id": event_id,
            "tenant": tenant,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "version": version,
            "payload": canonical_json(event),
            "now": now,
        },
    )
    return event_id


def _fact_projection(row):
    value = project(dict(row))
    for field in ("entities", "relations", "dates"):
        decoded = _decoded(value.get(field))
        value[field] = _public_facts(field, decoded if isinstance(decoded, list) else [])
    value.pop("tenant_id", None)
    value.pop("created_by", None)
    return value


def _public_facts(field, values):
    """Project the inference protocol's model observations to FactRevision facts."""
    result = []
    for item in values:
        if not isinstance(item, dict):
            continue
        if field == "entities":
            if {"entity_id", "source", "evidence"} <= set(item):
                result.append(item)
                continue
            candidates = item.get("candidates")
            candidates = candidates if isinstance(candidates, list) else []
            entity_id = (
                candidates[0].get("entity_id")
                if item.get("resolution") == "resolved"
                and len(candidates) == 1
                and isinstance(candidates[0], dict)
                else None
            )
            result.append(
                {
                    "detection_id": item.get("detection_id"),
                    "entity_id": entity_id,
                    "resolution": item.get("resolution", "unknown"),
                    "source": "model",
                    "evidence": [
                        {
                            "image_id": item.get("image_id"),
                            "detection_id": item.get("detection_id"),
                            "crop_id": None,
                        }
                    ],
                }
            )
        elif field == "relations":
            if {"same_location", "source", "evidence"} <= set(item):
                result.append(item)
                continue
            result.append(
                {
                    "source_detection_id": item.get("source_detection_id"),
                    "target_detection_id": item.get("target_detection_id"),
                    "relation": item.get("relation", "unknown"),
                    "same_location": None,
                    "source": "model",
                    "evidence": [
                        {
                            "image_id": item.get("image_id"),
                            "detection_id": item.get("source_detection_id"),
                            "crop_id": None,
                        }
                    ],
                }
            )
        elif field == "dates":
            if {"kind", "source", "evidence"} <= set(item):
                result.append(item)
                continue
            result.append(
                {
                    "detection_id": item.get("detection_id"),
                    "kind": item.get("kind", "unknown"),
                    "value": item.get("value"),
                    "source": "model",
                    "evidence": [
                        {
                            "image_id": item.get("image_id"),
                            "detection_id": item.get("detection_id"),
                            "crop_id": None,
                        }
                    ],
                }
            )
    return result


def _item(connection, tenant, item_id, *, lock=False):
    return (
        connection.execute(
            text(
                "SELECT i.id,i.tenant_id,i.laboratory_id,i.version,i.status,"
                "i.current_run_id,i.current_fact_revision_id,i.current_evaluation_id,"
                "ins.status AS inspection_status,ins.inspector_id AS creator_id "
                "FROM inspection_items i JOIN inspections ins ON ins.tenant_id=i.tenant_id "
                "AND ins.id=i.inspection_id AND ins.laboratory_id=i.laboratory_id "
                "WHERE i.tenant_id=:tenant AND i.id=:item" + (" FOR UPDATE" if lock else "")
            ),
            {"tenant": tenant, "item": item_id},
        )
        .mappings()
        .first()
    )


def _fact(connection, tenant, fact_id, *, lock=False):
    return (
        connection.execute(
            text(
                "SELECT id,created_at,updated_at,version,tenant_id,item_id,run_id,revision,"
                "entities,relations,dates,reason,created_by FROM fact_revisions "
                "WHERE tenant_id=:tenant AND id=:fact" + (" FOR UPDATE" if lock else "")
            ),
            {"tenant": tenant, "fact": fact_id},
        )
        .mappings()
        .first()
    )


def current_facts(connection, actor, item_id):
    row = _item(connection, actor.tenant_id, item_id)
    if row is None:
        raise not_found()
    authorize(actor, Permission.READ, row["laboratory_id"])
    if row["current_fact_revision_id"] is None:
        raise not_found()
    fact = _fact(connection, actor.tenant_id, row["current_fact_revision_id"])
    if fact is None or fact["item_id"] != item_id or fact["run_id"] != row["current_run_id"]:
        raise not_found()
    return _fact_projection(fact)


def authorize_item(connection, actor, item_id):
    row = _item(connection, actor.tenant_id, item_id)
    if row is None:
        raise not_found()
    authorize(actor, Permission.REVIEW, row["laboratory_id"])
    return row


def _task_projection(connection, tenant, task_id):
    row = (
        connection.execute(
            text(
                "SELECT id,created_at,updated_at,version,task_type,resource_id,state,attempt,"
                "replay_generation,available_at,last_error_code FROM task_runs "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": tenant, "id": task_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    value = project(row)
    value["available_at"] = timestamp(row["available_at"])
    return value


def _finding_evidence(raw, connection, tenant, run_id, *, ready_only=False):
    refs = []
    readiness = (
        "AND ai.status='ready' AND ai.analysis_key IS NOT NULL "
        "AND ai.analysis_object_version IS NOT NULL "
        if ready_only
        else ""
    )
    run_image_ids = list(
        connection.execute(
            text(
                "SELECT ri.image_id FROM run_images ri "
                "JOIN asset_images ai ON ai.tenant_id=ri.tenant_id AND ai.id=ri.image_id "
                "WHERE ri.tenant_id=:tenant AND ri.run_id=:run "
                + readiness
                + "ORDER BY ri.ordinal,ri.id"
            ),
            {"tenant": tenant, "run": run_id},
        ).scalars()
    )
    run_images = set(run_image_ids)
    raw = raw if isinstance(raw, dict) else {}
    raw_facts = raw.get("raw_facts") if isinstance(raw.get("raw_facts"), dict) else {}
    for source in (raw_facts.get("entities"), raw_facts.get("relations")):
        if not isinstance(source, list):
            continue
        for item in source:
            if not isinstance(item, dict):
                continue
            item_refs = item.get("evidence") if isinstance(item.get("evidence"), list) else [item]
            for ref in item_refs:
                if (
                    not isinstance(ref, dict)
                    or ref.get("image_id") is None
                    or ref["image_id"] not in run_images
                ):
                    continue
                refs.append(
                    {
                        "image_id": ref["image_id"],
                        "detection_id": ref.get("detection_id")
                        or item.get("detection_id")
                        or item.get("source_detection_id"),
                        "crop_id": ref.get("crop_id"),
                    }
                )
    if not refs:
        image_id = run_image_ids[0] if run_image_ids else None
        if image_id is not None:
            refs.append({"image_id": image_id, "detection_id": None, "crop_id": None})
    unique = []
    seen = set()
    for ref in refs:
        key = (ref["image_id"], ref["detection_id"], ref["crop_id"])
        if key not in seen:
            unique.append(ref)
            seen.add(key)
    if not unique:
        raise ServiceError("STATE_CONFLICT", 409, "Finding evidence is unavailable")
    return unique[:20]


def _finding_projection(connection, actor, row):
    value = project(dict(row))
    raw = _decoded(row.get("evidence"))
    value["evidence"] = _finding_evidence(raw, connection, actor.tenant_id, row["run_id"])
    value["task_id"] = row.get("task_id")
    value["allowed_actions"] = []
    try:
        authorize(actor, Permission.REVIEW, row["laboratory_id"])
        if row["superseded_at"] is None and row["status"] == "needs_review":
            value["allowed_actions"] = [
                "confirmFinding",
                "rejectFinding",
                "cannotdetermineFinding",
            ]
    except ServiceError:
        pass
    value.pop("tenant_id", None)
    value.pop("laboratory_id", None)
    value["superseded_at"] = timestamp(row["superseded_at"]) if row["superseded_at"] else None
    return value


_FINDING_COLUMNS = (
    "f.id,f.created_at,f.updated_at,f.version,f.tenant_id,f.item_id,f.run_id,"
    "f.fact_revision_id,f.rule_evaluation_id,f.rule_id,f.type,f.severity,f.status,"
    "f.explanation,f.evidence,f.superseded_at,f.confirmed_by,r.laboratory_id,"
    "rt.id AS task_id"
)


def _finding_row(connection, tenant, finding_id, *, lock=False):
    return (
        connection.execute(
            text(
                f"SELECT {_FINDING_COLUMNS} FROM findings f "
                "JOIN inference_runs r ON r.tenant_id=f.tenant_id AND r.item_id=f.item_id "
                "AND r.id=f.run_id LEFT JOIN remediation_tasks rt ON rt.tenant_id=f.tenant_id "
                "AND rt.finding_id=f.id WHERE f.tenant_id=:tenant AND f.id=:id"
                + (" FOR UPDATE" if lock else "")
            ),
            {"tenant": tenant, "id": finding_id},
        )
        .mappings()
        .first()
    )


def get_finding(connection, actor, finding_id):
    row = _finding_row(connection, actor.tenant_id, finding_id)
    if row is None:
        raise not_found()
    authorize(actor, Permission.READ, row["laboratory_id"])
    return _finding_projection(connection, actor, row)


def authorize_finding(connection, actor, finding_id):
    row = _finding_row(connection, actor.tenant_id, finding_id)
    if row is None:
        raise not_found()
    authorize(actor, Permission.REVIEW, row["laboratory_id"])
    return row


def list_findings(
    connection,
    actor,
    page,
    page_size,
    laboratory_id=None,
    current_only=True,
    status=None,
    severity=None,
):
    params = {"tenant": actor.tenant_id}
    where = "f.tenant_id=:tenant" + lab_filter(actor, "r.laboratory_id", params, laboratory_id)
    if current_only:
        where += " AND f.superseded_at IS NULL"
    if status is not None:
        where += " AND f.status=:status"
        params["status"] = status
    if severity is not None:
        where += " AND f.severity=:severity"
        params["severity"] = severity
    return page_query(
        connection,
        "findings f JOIN inference_runs r ON r.tenant_id=f.tenant_id AND r.item_id=f.item_id "
        "AND r.id=f.run_id LEFT JOIN remediation_tasks rt ON rt.tenant_id=f.tenant_id "
        "AND rt.finding_id=f.id",
        _FINDING_COLUMNS,
        where,
        params,
        page,
        page_size,
        project_row=lambda row: _finding_projection(connection, actor, row),
        order_by="f.created_at DESC,f.id DESC",
    )


def _finding_domain(row):
    return Finding(
        id=row["id"],
        tenant_id=row["tenant_id"],
        laboratory_id=row["laboratory_id"],
        version=row["version"],
        status=row["status"],
        item_id=row["item_id"],
        run_id=row["run_id"],
        evaluation_id=row["rule_evaluation_id"],
        superseded=row["superseded_at"] is not None,
        evidence_ready=bool(_decoded(row.get("evidence"))),
    )


def _validate_fact_snapshot(connection, tenant, item_id, run, body):
    """Validate a complete human fact replacement against the pinned run input."""
    arrays = {
        "entities": body.get("entities"),
        "relations": body.get("relations"),
        "dates": body.get("dates"),
    }
    if any(not isinstance(value, list) for value in arrays.values()):
        raise ServiceError("VALIDATION_ERROR", 422, "Facts must be complete arrays")

    result = _decoded(run.get("result"))
    result = result if isinstance(result, dict) else {}
    detections = result.get("detections") if isinstance(result.get("detections"), list) else []
    detection_ids = {
        value.get("detection_id")
        for value in detections
        if isinstance(value, dict) and isinstance(value.get("detection_id"), str)
    }
    detection_images = {value.get("detection_id"): value.get("image_id") for value in detections}
    image_ids = {
        value
        for value in connection.execute(
            text("SELECT image_id FROM run_images WHERE tenant_id=:tenant AND run_id=:run"),
            {"tenant": tenant, "run": run["id"]},
        ).scalars()
    }
    crop_rows = (
        connection.execute(
            text(
                "SELECT image_id,crop_id,detection_id FROM image_derivatives "
                "WHERE tenant_id=:tenant AND run_id=:run"
            ),
            {"tenant": tenant, "run": run["id"]},
        )
        .mappings()
        .all()
    )
    crops = {(row["image_id"], row["crop_id"], row["detection_id"]) for row in crop_rows}
    crop_ids = {row["crop_id"] for row in crop_rows}

    def evidence_refs(value):
        refs = value.get("evidence") if isinstance(value, dict) else None
        if not isinstance(refs, list) or not refs:
            raise ServiceError("VALIDATION_ERROR", 422, "Each fact needs evidence")
        seen = set()
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"image_id", "detection_id", "crop_id"}:
                raise ServiceError("VALIDATION_ERROR", 422, "Invalid fact evidence")
            image_id = ref["image_id"]
            detection_id = ref["detection_id"]
            crop_id = ref["crop_id"]
            if image_id not in image_ids:
                raise ServiceError(
                    "STATE_CONFLICT", 409, "Evidence image is not in the current run"
                )
            if detection_id is not None and (
                detection_id not in detection_ids or detection_images[detection_id] != image_id
            ):
                raise ServiceError(
                    "STATE_CONFLICT", 409, "Evidence detection is not in the current run"
                )
            if crop_id is not None:
                if crop_id not in crop_ids or not any(
                    row[0] == image_id and row[1] == crop_id and row[2] == detection_id
                    for row in crops
                ):
                    raise ServiceError(
                        "STATE_CONFLICT", 409, "Evidence crop is not in the current run"
                    )
            key = (image_id, detection_id, crop_id)
            if key in seen:
                raise ServiceError("VALIDATION_ERROR", 422, "Duplicate fact evidence")
            seen.add(key)

    entity_keys = set()
    for value in arrays["entities"]:
        if not isinstance(value, dict) or set(value) != {
            "detection_id",
            "entity_id",
            "resolution",
            "source",
            "evidence",
        }:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid entity fact")
        if value["detection_id"] not in detection_ids:
            raise ServiceError("STATE_CONFLICT", 409, "Entity detection is not in the current run")
        if value["source"] != "human":
            raise ServiceError("VALIDATION_ERROR", 422, "Fact edits must use human source")
        if value["resolution"] == "resolved" and value["entity_id"] is None:
            raise ServiceError("VALIDATION_ERROR", 422, "Resolved entity needs entity_id")
        if value["entity_id"] is not None:
            known_entity = connection.scalar(
                text(
                    "SELECT id FROM chemical_entities WHERE tenant_id=:tenant "
                    "AND dictionary_version_id=:dictionary AND id=:entity"
                ),
                {
                    "tenant": tenant,
                    "dictionary": run["dictionary_version_id"],
                    "entity": value["entity_id"],
                },
            )
            if known_entity is None:
                raise ServiceError("STATE_CONFLICT", 409, "Entity is outside the pinned dictionary")
        key = value["detection_id"]
        if key in entity_keys:
            raise ServiceError("VALIDATION_ERROR", 422, "Duplicate entity fact")
        entity_keys.add(key)
        evidence_refs(value)

    relation_keys = set()
    for value in arrays["relations"]:
        if not isinstance(value, dict) or set(value) != {
            "source_detection_id",
            "target_detection_id",
            "relation",
            "same_location",
            "source",
            "evidence",
        }:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid relation fact")
        if (
            value["source_detection_id"] not in detection_ids
            or value["target_detection_id"] not in detection_ids
        ):
            raise ServiceError(
                "STATE_CONFLICT", 409, "Relation detection is not in the current run"
            )
        if value["source"] != "human":
            raise ServiceError("VALIDATION_ERROR", 422, "Fact edits must use human source")
        if value["source_detection_id"] == value["target_detection_id"]:
            raise ServiceError("VALIDATION_ERROR", 422, "Relation endpoints must differ")
        if value["same_location"] is not True and value["relation"] != "unknown":
            raise ServiceError(
                "VALIDATION_ERROR", 422, "Known adjacency requires same_location=true"
            )
        key = (value["source_detection_id"], value["target_detection_id"])
        if key in relation_keys:
            raise ServiceError("VALIDATION_ERROR", 422, "Duplicate relation fact")
        relation_keys.add(key)
        evidence_refs(value)

    date_keys = set()
    for value in arrays["dates"]:
        if not isinstance(value, dict) or set(value) != {
            "detection_id",
            "kind",
            "value",
            "source",
            "evidence",
        }:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid date fact")
        if value["detection_id"] not in detection_ids:
            raise ServiceError("STATE_CONFLICT", 409, "Date detection is not in the current run")
        if value["source"] != "human":
            raise ServiceError("VALIDATION_ERROR", 422, "Fact edits must use human source")
        if value["value"] is not None:
            try:
                date.fromisoformat(value["value"])
            except (TypeError, ValueError):
                raise ServiceError("VALIDATION_ERROR", 422, "Invalid fact date") from None
        key = (value["detection_id"], value["kind"])
        if key in date_keys:
            raise ServiceError("VALIDATION_ERROR", 422, "Duplicate date fact")
        date_keys.add(key)
        evidence_refs(value)


def decide_finding(connection, actor, finding_id, body, operation, request_id):
    row = _finding_row(connection, actor.tenant_id, finding_id, lock=True)
    if row is None:
        raise not_found()
    authorize(actor, Permission.REVIEW, row["laboratory_id"])
    finding = _finding_domain(row)
    evidence = _finding_evidence(
        _decoded(row.get("evidence")),
        connection,
        actor.tenant_id,
        row["run_id"],
        ready_only=True,
    )
    finding = Finding(
        id=finding.id,
        tenant_id=finding.tenant_id,
        laboratory_id=finding.laboratory_id,
        version=finding.version,
        status=finding.status,
        item_id=finding.item_id,
        run_id=finding.run_id,
        evaluation_id=finding.evaluation_id,
        superseded=finding.superseded,
        evidence_ready=bool(evidence),
    )
    expected = body.get("expected_version")
    reason = body.get("reason")
    if operation == "confirm":
        new_status = confirm_finding(actor, finding, expected_version=expected, reason=reason)
    elif operation == "reject":
        new_status = reject_finding(actor, finding, expected_version=expected, reason=reason)
    elif operation == "cannot-determine":
        new_status = cannot_determine_finding(
            actor,
            finding,
            expected_version=expected,
            reason=reason,
            reason_code=body.get("reason_code"),
        )
    else:
        raise ServiceError("VALIDATION_ERROR", 422, "Unknown finding decision")
    now = db_now(connection)
    updated = connection.execute(
        text(
            "UPDATE findings SET status=:status,confirmed_by=:confirmed,version=version+1,"
            "updated_at=:now WHERE tenant_id=:tenant AND id=:id AND version=:version "
            "AND superseded_at IS NULL"
        ),
        {
            "status": str(new_status),
            "confirmed": actor.user_id if str(new_status) == "confirmed" else None,
            "now": now,
            "tenant": actor.tenant_id,
            "id": finding_id,
            "version": expected,
        },
    ).rowcount
    if updated != 1:
        raise ServiceError("VERSION_CONFLICT", 409, "Refresh the finding before retrying")
    event_type = {
        "confirmed": "FindingConfirmed",
        "rejected": "FindingRejected",
    }.get(str(new_status))
    if event_type is not None:
        _domain_event(
            connection,
            tenant=actor.tenant_id,
            event_type=event_type,
            aggregate_type="finding",
            aggregate_id=finding_id,
            version=expected + 1,
            payload={"finding_id": finding_id, "actor_id": actor.user_id},
            now=now,
        )
    connection.execute(
        text(
            "INSERT INTO review_actions (id,tenant_id,actor_id,resource_type,resource_id,action,"
            "reason,before_version,after_version) VALUES (:id,:tenant,:actor,'finding',:resource,"
            ":action,:reason,:before,:after)"
        ),
        {
            "id": str(uuid4()),
            "tenant": actor.tenant_id,
            "actor": actor.user_id,
            "resource": finding_id,
            "action": f"finding.{operation}",
            "reason": reason,
            "before": expected,
            "after": expected + 1,
        },
    )
    append_audit(
        connection,
        principal=actor,
        action=f"finding.{operation}",
        resource_type="finding",
        resource_id=finding_id,
        changes={
            "status": str(new_status),
            "version": expected + 1,
            **({"reason_code": body.get("reason_code")} if operation == "cannot-determine" else {}),
        },
        reason=reason,
        request_id=request_id,
    )
    return _finding_projection(
        connection, actor, _finding_row(connection, actor.tenant_id, finding_id)
    )


def edit_facts(connection, actor, item_id, body, request_id):
    require_transaction(connection)
    row = _item(connection, actor.tenant_id, item_id, lock=True)
    if row is None:
        raise not_found()
    authorize(actor, Permission.REVIEW, row["laboratory_id"])
    require_version(row["version"], body.get("expected_version"))
    if row["current_run_id"] is None or row["current_evaluation_id"] is None:
        raise not_found()
    if row["status"] != "needs_review":
        raise ServiceError("STATE_CONFLICT", 409, "Facts can only be edited during review")
    run = (
        connection.execute(
            text(
                "SELECT * FROM inference_runs WHERE tenant_id=:tenant AND id=:run "
                "AND item_id=:item FOR UPDATE"
            ),
            {
                "tenant": actor.tenant_id,
                "run": row["current_run_id"],
                "item": item_id,
            },
        )
        .mappings()
        .first()
    )
    current_findings = (
        connection.execute(
            text(
                "SELECT id,status FROM findings WHERE tenant_id=:tenant AND item_id=:item "
                "AND run_id=:run AND rule_evaluation_id=:evaluation AND superseded_at IS NULL "
                "ORDER BY id FOR UPDATE"
            ),
            {
                "tenant": actor.tenant_id,
                "item": item_id,
                "run": row["current_run_id"],
                "evaluation": row["current_evaluation_id"],
            },
        )
        .mappings()
        .all()
    )
    if any(value["status"] in {"confirmed", "dispatched", "closed"} for value in current_findings):
        raise ServiceError("STATE_CONFLICT", 409, "Confirmed findings block fact revision")
    evaluation = (
        connection.execute(
            text(
                "SELECT * FROM rule_evaluations WHERE tenant_id=:tenant AND id=:evaluation "
                "AND item_id=:item AND run_id=:run FOR UPDATE"
            ),
            {
                "tenant": actor.tenant_id,
                "evaluation": row["current_evaluation_id"],
                "item": item_id,
                "run": row["current_run_id"],
            },
        )
        .mappings()
        .first()
    )
    if (
        run is None
        or run["status"] != "needs_review"
        or run["stage"] != "done"
        or evaluation is None
        or evaluation["status"] != "completed"
    ):
        raise ServiceError("STATE_CONFLICT", 409, "Current evaluation is not complete")
    _validate_fact_snapshot(connection, actor.tenant_id, item_id, run, body)
    now = db_now(connection)
    revision = connection.scalar(
        text(
            "SELECT COALESCE(MAX(revision),0)+1 FROM fact_revisions "
            "WHERE tenant_id=:tenant AND run_id=:run"
        ),
        {"tenant": actor.tenant_id, "run": run["id"]},
    )
    fact_id = str(uuid4())
    connection.execute(
        text(
            "INSERT INTO fact_revisions (id,tenant_id,item_id,run_id,revision,entities,relations,"
            "dates,reason,created_by) VALUES (:id,:tenant,:item,:run,:revision,"
            ":entities,:relations,:dates,:reason,:actor)"
        ),
        {
            "id": fact_id,
            "tenant": actor.tenant_id,
            "item": item_id,
            "run": run["id"],
            "revision": revision,
            "entities": canonical_json(body["entities"]),
            "relations": canonical_json(body["relations"]),
            "dates": canonical_json(body["dates"]),
            "reason": body["reason"],
            "actor": actor.user_id,
        },
    )
    connection.execute(
        text(
            "UPDATE rule_evaluations SET status='superseded',version=version+1,updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:evaluation"
        ),
        {"tenant": actor.tenant_id, "evaluation": evaluation["id"], "now": now},
    )
    connection.execute(
        text(
            "UPDATE findings SET superseded_at=:now,version=version+1,updated_at=:now "
            "WHERE tenant_id=:tenant AND item_id=:item AND superseded_at IS NULL"
        ),
        {"tenant": actor.tenant_id, "item": item_id, "now": now},
    )
    new_evaluation = str(uuid4())
    connection.execute(
        text(
            "INSERT INTO rule_evaluations (id,tenant_id,item_id,run_id,fact_revision_id,"
            "rule_bundle_id,reference_date,status) VALUES (:id,:tenant,:item,:run,:fact,:bundle,"
            ":reference,'queued')"
        ),
        {
            "id": new_evaluation,
            "tenant": actor.tenant_id,
            "item": item_id,
            "run": run["id"],
            "fact": fact_id,
            "bundle": run["rule_bundle_id"],
            "reference": run["reference_date"],
        },
    )
    task_id = str(uuid4())
    payload = {
        "run_id": run["id"],
        "fact_revision_id": fact_id,
        "rule_bundle_id": run["rule_bundle_id"],
    }
    connection.execute(
        text(
            "INSERT INTO task_runs (id,tenant_id,task_type,resource_id,logical_key,payload,state,"
            "attempt,replay_generation,dispatch_sequence,fencing_token,available_at,created_at,"
            "updated_at) VALUES (:id,:tenant,'rule_evaluation',:run,:logical,:payload,'ready',"
            "0,0,1,0,:now,:now,:now)"
        ),
        {
            "id": task_id,
            "tenant": actor.tenant_id,
            "run": run["id"],
            "logical": f"rule_evaluation:{new_evaluation}",
            "payload": canonical_json(payload),
            "now": now,
        },
    )
    event = {
        "schema_version": "1.1",
        "tenant_id": actor.tenant_id,
        "trace_id": uuid4().hex,
        "task_id": task_id,
        "task_type": "rule_evaluation",
        "resource_id": run["id"],
        "replay_generation": 0,
        "dispatch_sequence": 1,
        "created_at": timestamp(now),
        "payload": payload,
    }
    connection.execute(
        text(
            "INSERT INTO outbox_events (id,tenant_id,event_type,aggregate_type,aggregate_id,"
            "aggregate_version,payload,state,available_at,created_at,updated_at) VALUES "
            "(:id,:tenant,'TaskDispatch','task_run',:task,1,:payload,'pending',:now,:now,:now)"
        ),
        {
            "id": str(uuid4()),
            "tenant": actor.tenant_id,
            "task": task_id,
            "payload": canonical_json(event),
            "now": now,
        },
    )
    updated = connection.execute(
        text(
            "UPDATE inference_runs SET status='processing',stage='rules',finished_at=NULL,"
            "updated_at=:now,version=version+1 WHERE tenant_id=:tenant AND id=:run"
        ),
        {"tenant": actor.tenant_id, "run": run["id"], "now": now},
    ).rowcount
    if updated != 1:
        raise ServiceError("STATE_CONFLICT", 409, "Current inference run changed during facts edit")
    updated = connection.execute(
        text(
            "UPDATE inspection_items SET status='processing',current_fact_revision_id=:fact,"
            "current_evaluation_id=:evaluation,review_outcome=NULL,reviewed_by=NULL,"
            "reviewed_at=NULL,version=version+1,updated_at=:now WHERE tenant_id=:tenant "
            "AND id=:item AND version=:version"
        ),
        {
            "tenant": actor.tenant_id,
            "item": item_id,
            "fact": fact_id,
            "evaluation": new_evaluation,
            "now": now,
            "version": body.get("expected_version"),
        },
    ).rowcount
    if updated != 1:
        raise ServiceError("VERSION_CONFLICT", 409, "Refresh the item before retrying")
    _domain_event(
        connection,
        tenant=actor.tenant_id,
        event_type="FactsRevised",
        aggregate_type="inspection_item",
        aggregate_id=item_id,
        version=body.get("expected_version") + 1,
        payload={
            "run_id": run["id"],
            "fact_revision_id": fact_id,
            "rule_bundle_id": run["rule_bundle_id"],
        },
        now=now,
    )
    append_audit(
        connection,
        principal=actor,
        action="inspection_item.facts_edit",
        resource_type="inspection_item",
        resource_id=item_id,
        changes={
            "fact_revision_id": fact_id,
            "evaluation_id": new_evaluation,
            "status": "processing",
        },
        reason=body["reason"],
        request_id=request_id,
    )
    return _task_projection(connection, actor.tenant_id, task_id)
