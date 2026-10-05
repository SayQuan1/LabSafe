"""Atomic submitInspectionItem command persistence.

The command loads every piece of state from MySQL under the documented lock
order, then creates the immutable inference run, run image selection, durable
task and dispatch outbox row in one transaction.  No client supplied model or
tenant identifiers are trusted.
"""

import hashlib
from uuid import uuid4

from sqlalchemy import text

from packages.domain.security import Permission, authorize, not_found
from packages.domain.workflow import (
    FailedRun,
    Finding,
    Image,
    ImageSelection,
    Item,
    PinnedInputs,
    submit_item,
)
from packages.persistence.security import append_audit
from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json


def _item(connection, tenant, item_id, *, lock=True):
    suffix = " FOR UPDATE" if lock else ""
    return (
        connection.execute(
            text(
                "SELECT item.id,item.version,item.tenant_id,item.laboratory_id,item.location_id,"
                "item.status,item.current_run_id,item.current_evaluation_id,item.submission_revision,"
                "inspection.status AS inspection_status,inspection.inspector_id AS creator_id "
                "FROM inspection_items item JOIN inspections inspection ON "
                "inspection.tenant_id=item.tenant_id AND inspection.id=item.inspection_id "
                "AND inspection.laboratory_id=item.laboratory_id "
                "WHERE item.tenant_id=:tenant AND item.id=:id" + suffix
            ),
            {"tenant": tenant, "id": item_id},
        )
        .mappings()
        .first()
    )


def _activation(connection, tenant, laboratory):
    row = (
        connection.execute(
            text(
                "SELECT a.id,a.model_bundle_id,a.dictionary_version_id,a.rule_bundle_id,"
                "a.pipeline_version,a.device_profile,m.status AS model_status,"
                "d.status AS dictionary_status FROM activations a "
                "JOIN model_versions m ON m.tenant_id=a.tenant_id AND m.id=a.model_bundle_id "
                "AND m.dictionary_version_id=a.dictionary_version_id "
                "JOIN dictionary_versions d ON d.tenant_id=a.tenant_id "
                "AND d.id=a.dictionary_version_id "
                "WHERE a.tenant_id=:tenant AND a.laboratory_id=:lab FOR SHARE"
            ),
            {"tenant": tenant, "lab": laboratory},
        )
        .mappings()
        .first()
    )
    if row is None or row["model_status"] != "published" or row["dictionary_status"] != "published":
        raise not_found()
    return row


def _finding_rows(connection, tenant, item_id, laboratory_id):
    rows = (
        connection.execute(
            text(
                "SELECT id,tenant_id,item_id,run_id,"
                "rule_evaluation_id AS evaluation_id,"
                "status,superseded_at FROM findings WHERE tenant_id=:tenant AND item_id=:item "
                "ORDER BY id FOR UPDATE"
            ),
            {"tenant": tenant, "item": item_id},
        )
        .mappings()
        .all()
    )
    return tuple(
        Finding(
            id=row["id"],
            tenant_id=row["tenant_id"],
            laboratory_id=laboratory_id,
            version=1,
            item_id=row["item_id"],
            run_id=row["run_id"],
            evaluation_id=row["evaluation_id"],
            status=row["status"],
            superseded=row["superseded_at"] is not None,
            evidence_ready=True,
        )
        for row in rows
    )


def _images(connection, tenant, item_id, selections):
    rows = (
        connection.execute(
            text(
                "SELECT id,tenant_id,laboratory_id,inspection_item_id,status,analysis_sha256 "
                "FROM asset_images WHERE tenant_id=:tenant AND inspection_item_id=:item "
                "ORDER BY id FOR UPDATE"
            ),
            {"tenant": tenant, "item": item_id},
        )
        .mappings()
        .all()
    )
    by_id = {row["id"]: row for row in rows}
    loaded = []
    for selection in selections:
        row = by_id.get(selection.image_id)
        if row is None:
            raise not_found()
        loaded.append(
            Image(
                id=row["id"],
                tenant_id=row["tenant_id"],
                laboratory_id=row["laboratory_id"],
                owner_type="inspection_item",
                owner_id=item_id,
                location_id=None,
                status=row["status"],
                available=row["analysis_sha256"] is not None,
            )
        )
    return tuple(loaded), tuple(by_id[s.image_id] for s in selections)


def _failed_run(connection, tenant, item_id, run_id):
    row = (
        connection.execute(
            text(
                "SELECT * FROM inference_runs WHERE tenant_id=:tenant AND item_id=:item "
                "AND id=:run FOR UPDATE"
            ),
            {"tenant": tenant, "item": item_id, "run": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    images = (
        connection.execute(
            text(
                "SELECT image_id,role,parent_image_id FROM run_images "
                "WHERE tenant_id=:tenant AND item_id=:item AND run_id=:run "
                "ORDER BY ordinal FOR UPDATE"
            ),
            {"tenant": tenant, "item": item_id, "run": run_id},
        )
        .mappings()
        .all()
    )
    selections = tuple(
        ImageSelection(
            image_id=value["image_id"],
            role=value["role"],
            parent_image_id=value["parent_image_id"],
        )
        for value in images
    )
    config = (
        connection.execute(
            text(
                "SELECT m.id AS model_id,m.status AS model_status,d.id AS dictionary_id,"
                "d.status AS dictionary_status,b.id AS rule_id "
                "FROM model_versions m JOIN dictionary_versions d "
                "ON d.tenant_id=m.tenant_id AND d.id=m.dictionary_version_id "
                "JOIN rule_bundles b ON b.tenant_id=m.tenant_id AND b.id=:rules "
                "WHERE m.tenant_id=:tenant AND m.id=:model AND d.id=:dictionary"
            ),
            {
                "tenant": tenant,
                "model": row["model_bundle_id"],
                "dictionary": row["dictionary_version_id"],
                "rules": row["rule_bundle_id"],
            },
        )
        .mappings()
        .first()
    )
    available = config is not None and config["model_status"] != "retired"
    pinned = PinnedInputs(
        model_bundle_id=row["model_bundle_id"],
        dictionary_version_id=row["dictionary_version_id"],
        rule_bundle_id=row["rule_bundle_id"],
        pipeline_version=row["pipeline_version"],
        device_profile=row["device_profile"],
        reference_date=row["reference_date"],
        images=selections,
    )
    return row, FailedRun(
        id=row["id"],
        tenant_id=row["tenant_id"],
        item_id=row["item_id"],
        status=row["status"],
        pinned=pinned,
        configuration_available=available,
    )


def _write_pipeline_task(
    connection, *, tenant, run_id, item_id, revision, pinned, replay_of, now, trace
):
    task_id = str(uuid4())
    payload = {
        "run_id": run_id,
        "submission_revision": revision,
        "model_bundle_id": pinned.model_bundle_id,
        "dictionary_version_id": pinned.dictionary_version_id,
        "rule_bundle_id": pinned.rule_bundle_id,
    }
    connection.execute(
        text(
            "INSERT INTO task_runs (id,tenant_id,task_type,resource_id,logical_key,payload,state,"
            "attempt,replay_generation,dispatch_sequence,fencing_token,available_at,created_at,"
            "updated_at) VALUES (:id,:tenant,'inference_pipeline',:run,:logical,:payload,'ready',"
            "0,0,1,0,:now,:now,:now)"
        ),
        {
            "id": task_id,
            "tenant": tenant,
            "run": run_id,
            "logical": f"inference_pipeline:{run_id}",
            "payload": canonical_json(payload),
            "now": now,
        },
    )
    event = {
        "schema_version": "1.1",
        "tenant_id": tenant,
        "trace_id": trace.replace("-", ""),
        "task_id": task_id,
        "task_type": "inference_pipeline",
        "resource_id": run_id,
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
            "tenant": tenant,
            "task": task_id,
            "payload": canonical_json(event),
            "now": now,
        },
    )
    return task_id


def _projection(connection, tenant, run_id):
    row = (
        connection.execute(
            text(
                "SELECT r.id,r.created_at,r.updated_at,r.version,r.item_id,r.status,r.stage,"
                "r.submission_revision,r.model_bundle_id,r.dictionary_version_id,r.rule_bundle_id,"
                "r.pipeline_version,r.result_hash,r.error_code,t.id AS job_id "
                "FROM inference_runs r JOIN task_runs t ON t.tenant_id=r.tenant_id "
                "AND t.resource_id=r.id AND t.task_type='inference_pipeline' "
                "WHERE r.tenant_id=:tenant AND r.id=:id"
            ),
            {"tenant": tenant, "id": run_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    images = (
        connection.execute(
            text(
                "SELECT image_id FROM run_images WHERE tenant_id=:tenant AND run_id=:run "
                "ORDER BY ordinal"
            ),
            {"tenant": tenant, "run": run_id},
        )
        .scalars()
        .all()
    )
    value = dict(row)
    value["created_at"] = timestamp(value["created_at"])
    value["updated_at"] = timestamp(value["updated_at"])
    value["input_image_ids"] = list(images)
    value["is_simulated"] = True
    return value


def submit(connection, actor, item_id, expected_version, selections, request_id):
    require_transaction(connection)
    row = _item(connection, actor.tenant_id, item_id)
    if row is None:
        raise not_found()
    item = Item(
        id=row["id"],
        tenant_id=row["tenant_id"],
        laboratory_id=row["laboratory_id"],
        version=row["version"],
        status=row["status"],
        inspection_status=row["inspection_status"],
        creator_id=row["creator_id"],
        location_id=row["location_id"],
        current_run_id=row["current_run_id"],
        current_evaluation_id=row["current_evaluation_id"],
    )
    authorize(actor, Permission.CAPTURE, item.laboratory_id, creator_id=item.creator_id)
    findings = _finding_rows(connection, actor.tenant_id, item_id, item.laboratory_id)
    loaded, image_rows = _images(connection, actor.tenant_id, item_id, selections)
    # The image schema binds images to the item and laboratory.  Location is
    # therefore the item's server-owned location, never a client field.
    loaded = tuple(
        Image(
            id=image.id,
            tenant_id=image.tenant_id,
            laboratory_id=image.laboratory_id,
            owner_type=image.owner_type,
            owner_id=image.owner_id,
            location_id=item.location_id,
            status=image.status,
            available=image.available,
        )
        for image in loaded
    )
    activation = _activation(connection, actor.tenant_id, item.laboratory_id)
    submit_item(
        actor,
        item,
        expected_version=expected_version,
        images=loaded,
        selections=tuple(selections),
        current_findings=findings,
    )
    now = connection.scalar(text("SELECT UTC_TIMESTAMP(3)"))
    old_run = item.current_run_id
    old_evaluation = item.current_evaluation_id
    if old_run:
        connection.execute(
            text(
                "UPDATE inference_runs SET status='superseded',version=version+1,updated_at=:now "
                "WHERE tenant_id=:tenant AND item_id=:item AND id=:run "
                "AND status NOT IN ('superseded')"
            ),
            {"tenant": actor.tenant_id, "item": item.id, "run": old_run, "now": now},
        )
        if old_evaluation:
            connection.execute(
                text(
                    "UPDATE rule_evaluations SET status='superseded',version=version+1,"
                    "updated_at=:now "
                    "WHERE tenant_id=:tenant AND item_id=:item AND id=:evaluation "
                    "AND status NOT IN ('superseded')"
                ),
                {
                    "tenant": actor.tenant_id,
                    "item": item.id,
                    "evaluation": old_evaluation,
                    "now": now,
                },
            )
        connection.execute(
            text(
                "UPDATE findings SET superseded_at=:now,version=version+1,updated_at=:now "
                "WHERE tenant_id=:tenant AND item_id=:item AND superseded_at IS NULL"
            ),
            {"tenant": actor.tenant_id, "item": item.id, "now": now},
        )
    run_id, task_id = str(uuid4()), str(uuid4())
    revision = row["submission_revision"] + 1
    pinned_images = [
        {
            "image_id": selection.image_id,
            "role": selection.role,
            "parent_image_id": selection.parent_image_id,
        }
        for selection in selections
    ]
    reference_date = now.date()
    config = {
        "model_bundle_id": activation["model_bundle_id"],
        "dictionary_version_id": activation["dictionary_version_id"],
        "rule_bundle_id": activation["rule_bundle_id"],
        "pipeline_version": activation["pipeline_version"],
        "device_profile": activation["device_profile"],
        "reference_date": reference_date.isoformat(),
        "images": pinned_images,
    }
    input_hash = hashlib.sha256(canonical_json(config).encode()).hexdigest()
    connection.execute(
        text(
            "INSERT INTO inference_runs (id,tenant_id,item_id,laboratory_id,submission_revision,"
            "input_hash,model_bundle_id,dictionary_version_id,rule_bundle_id,pipeline_version,"
            "device_profile,reference_date,status,stage,created_at,updated_at) VALUES "
            "(:id,:tenant,:item,:lab,:revision,:hash,:model,:dictionary,:rules,:pipeline,:device,:ref,"
            "'queued','queued',:now,:now)"
        ),
        {
            "id": run_id,
            "tenant": actor.tenant_id,
            "item": item.id,
            "lab": item.laboratory_id,
            "revision": revision,
            "hash": input_hash,
            "model": activation["model_bundle_id"],
            "dictionary": activation["dictionary_version_id"],
            "rules": activation["rule_bundle_id"],
            "pipeline": activation["pipeline_version"],
            "device": activation["device_profile"],
            "ref": reference_date,
            "now": now,
        },
    )
    for ordinal, (selection, image) in enumerate(zip(selections, image_rows)):
        connection.execute(
            text(
                "INSERT INTO run_images (id,tenant_id,item_id,run_id,image_id,ordinal,role,"
                "parent_image_id,analysis_sha256) "
                "VALUES (:id,:tenant,:item,:run,:image,:ordinal,:role,:parent,:sha)"
            ),
            {
                "id": str(uuid4()),
                "tenant": actor.tenant_id,
                "item": item.id,
                "run": run_id,
                "image": image["id"],
                "ordinal": ordinal,
                "role": selection.role,
                "parent": selection.parent_image_id,
                "sha": image["analysis_sha256"],
            },
        )
    payload = {
        "run_id": run_id,
        "submission_revision": revision,
        "model_bundle_id": activation["model_bundle_id"],
        "dictionary_version_id": activation["dictionary_version_id"],
        "rule_bundle_id": activation["rule_bundle_id"],
    }
    connection.execute(
        text(
            "INSERT INTO task_runs (id,tenant_id,task_type,resource_id,logical_key,payload,state,"
            "attempt,replay_generation,dispatch_sequence,fencing_token,available_at,created_at,"
            "updated_at) VALUES (:id,:tenant,'inference_pipeline',:run,:logical,:payload,'ready',"
            "0,0,1,0,:now,:now,:now)"
        ),
        {
            "id": task_id,
            "tenant": actor.tenant_id,
            "run": run_id,
            "logical": f"inference_pipeline:{run_id}",
            "payload": canonical_json(payload),
            "now": now,
        },
    )
    event = {
        "schema_version": "1.1",
        "tenant_id": actor.tenant_id,
        "trace_id": request_id.replace("-", ""),
        "task_id": task_id,
        "task_type": "inference_pipeline",
        "resource_id": run_id,
        "replay_generation": 0,
        "dispatch_sequence": 1,
        "created_at": timestamp(now),
        "payload": payload,
    }
    connection.execute(
        text(
            "INSERT INTO outbox_events (id,tenant_id,event_type,aggregate_type,aggregate_id,"
            "aggregate_version,payload,state,available_at,created_at,updated_at) "
            "VALUES (:id,:tenant,'TaskDispatch','task_run',:task,1,:payload,'pending',"
            ":now,:now,:now)"
        ),
        {
            "id": str(uuid4()),
            "tenant": actor.tenant_id,
            "task": task_id,
            "payload": canonical_json(event),
            "now": now,
        },
    )
    connection.execute(
        text(
            "UPDATE inspection_items SET status='queued',submission_revision=:revision,"
            "current_run_id=:run,"
            "current_fact_revision_id=NULL,current_evaluation_id=NULL,review_outcome=NULL,reviewed_by=NULL,"
            "reviewed_at=NULL,version=version+1,updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:item AND version=:version"
        ),
        {
            "tenant": actor.tenant_id,
            "item": item.id,
            "revision": revision,
            "run": run_id,
            "now": now,
            "version": item.version,
        },
    )
    append_audit(
        connection,
        principal=actor,
        action="inspection_item.submit",
        resource_type="inspection_item",
        resource_id=item.id,
        changes={
            "status": "queued",
            "run_id": run_id,
            "submission_revision": revision,
        },
        reason="Submit inspection item for inference",
        request_id=request_id,
    )
    return _projection(connection, actor.tenant_id, run_id)
