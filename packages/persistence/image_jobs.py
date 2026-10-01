"""Atomic validate_image intent + initial dispatch, not a publisher or worker."""

from uuid import UUID, uuid4

from sqlalchemy import text

from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json


def enqueue_image_validation(connection, *, tenant_id, upload_id, image_id, request_id):
    require_transaction(connection)
    now = connection.scalar(text("SELECT UTC_TIMESTAMP(3)"))
    task_id, event_id = str(uuid4()), str(uuid4())
    payload = {"upload_id": upload_id, "image_id": image_id}
    connection.execute(
        text(
            "INSERT INTO task_runs (id,tenant_id,task_type,resource_id,logical_key,payload,"
            "state,attempt,replay_generation,dispatch_sequence,fencing_token,available_at,"
            "created_at,updated_at) VALUES "
            "(:id,:tenant,'validate_image',:image,:logical,:payload,"
            "'ready',0,0,1,0,:now,:now,:now)"
        ),
        {
            "id": task_id,
            "tenant": tenant_id,
            "image": image_id,
            "logical": f"validate_image:{image_id}",
            "payload": canonical_json(payload),
            "now": now,
        },
    )
    message = {
        "schema_version": "1.1",
        "tenant_id": tenant_id,
        "trace_id": UUID(request_id).hex,
        "task_id": task_id,
        "task_type": "validate_image",
        "resource_id": image_id,
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
            "id": event_id,
            "tenant": tenant_id,
            "task": task_id,
            "payload": canonical_json(message),
            "now": now,
        },
    )
    return task_id
