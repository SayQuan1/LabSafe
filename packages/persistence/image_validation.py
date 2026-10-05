"""Server-owned image outcome callback inside the execution fencing transaction."""

from types import SimpleNamespace
from uuid import UUID, uuid4

from sqlalchemy import text

from packages.domain.image_validation import RejectedImage, ValidatedImage, validate_result
from packages.persistence.dispatch import db_now, decoded
from packages.persistence.security import append_audit
from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json


def write_image_result(connection, tenant, task_id, input, outcome):
    """Call ONLY through ImageExecution.commit/execute, after owner/input locks."""
    require_transaction(connection)
    params = {"tenant": tenant, "id": input.image_id}
    ready = isinstance(outcome, ValidatedImage)
    if ready:
        validate_result(tenant, input, outcome)
        connection.execute(
            text(
                "UPDATE asset_images SET status='ready',original_key=:original,"
                "original_object_version=:original_version,analysis_key=:analysis,"
                "analysis_object_version=:analysis_version,analysis_sha256=:sha,"
                "width=:width,height=:height,version=version+1,updated_at=UTC_TIMESTAMP(3) "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {
                **params,
                "original": outcome.original_key,
                "original_version": outcome.original_version,
                "analysis": outcome.analysis_key,
                "analysis_version": outcome.analysis_version,
                "sha": outcome.analysis_sha256,
                "width": outcome.width,
                "height": outcome.height,
            },
        )
    elif isinstance(outcome, RejectedImage):
        connection.execute(
            text(
                "UPDATE asset_images SET status='rejected',analysis_key=NULL,"
                "analysis_object_version=NULL,analysis_sha256=NULL,width=NULL,height=NULL,"
                "version=version+1,updated_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant AND id=:id"
            ),
            params,
        )
    else:
        raise ValueError("Invalid image outcome")
    status = "ready" if ready else "rejected"
    connection.execute(
        text(
            "UPDATE uploads SET status=:status,version=version+1,updated_at=UTC_TIMESTAMP(3) "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {"tenant": tenant, "id": input.upload_id, "status": status},
    )
    if input.owner_type == "inspection_item":
        owner_params = {"tenant": tenant, "id": input.owner_id}
        if ready:
            connection.execute(
                text(
                    "UPDATE inspection_items SET status='uploaded',version=version+1,"
                    "updated_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant AND id=:id "
                    "AND status='draft'"
                ),
                owner_params,
            )
            connection.execute(
                text(
                    "UPDATE inspections SET status='in_progress',version=version+1,"
                    "updated_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant AND status='draft' "
                    "AND id=(SELECT inspection_id FROM inspection_items "
                    "WHERE tenant_id=:tenant AND id=:id)"
                ),
                owner_params,
            )
        else:
            # The owner mutex held by commit serializes validators and upload completion.
            has_ready = connection.scalar(
                text(
                    "SELECT id FROM asset_images WHERE tenant_id=:tenant "
                    "AND inspection_item_id=:id AND status='ready' LIMIT 1"
                ),
                owner_params,
            )
            connection.execute(
                text(
                    "UPDATE inspection_items SET status=:status,version=version+1,"
                    "updated_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant AND id=:id "
                    "AND status<>:status"
                ),
                {**owner_params, "status": "uploaded" if has_ready else "draft"},
            )
    # A service execution has no human actor; never impersonate the uploading user.
    source = connection.scalar(
        text(
            "SELECT payload FROM outbox_events WHERE tenant_id=:tenant "
            "AND aggregate_id=:task AND event_type='TaskDispatch' ORDER BY created_at,id LIMIT 1"
        ),
        {"tenant": tenant, "task": task_id},
    )
    trace = decoded(source)["trace_id"] if source else UUID(task_id).hex
    append_audit(
        connection,
        principal=SimpleNamespace(tenant_id=tenant, user_id=None),
        action="image.validate",
        resource_type="image",
        resource_id=input.image_id,
        changes={
            "status": status,
            "error_code": None if ready else outcome.code,
            "owner_type": input.owner_type,
            "owner_id": input.owner_id,
        },
        reason="Validate fixed-version upload",
        request_id=str(UUID(trace)),
    )
    if ready:
        now, event_id = db_now(connection), str(uuid4())
        version = input.image_version + 1
        event = {
            "schema_version": "1.1",
            "tenant_id": tenant,
            "trace_id": trace,
            "event_id": event_id,
            "event_type": "ImageValidated",
            "aggregate_type": "image",
            "aggregate_id": input.image_id,
            "aggregate_version": version,
            "occurred_at": timestamp(now),
            "payload": {
                "image_id": input.image_id,
                "owner_id": input.owner_id,
                "owner_type": input.owner_type,
                "analysis_sha256": outcome.analysis_sha256,
            },
        }
        connection.execute(
            text(
                "INSERT INTO outbox_events (id,tenant_id,event_type,aggregate_type,aggregate_id,"
                "aggregate_version,payload,state,available_at,created_at,updated_at) VALUES "
                "(:event,:tenant,'ImageValidated','image',:id,:version,:payload,'pending',:now,:now,:now)"
            ),
            {
                **params,
                "event": event_id,
                "version": version,
                "payload": canonical_json(event),
                "now": now,
            },
        )
