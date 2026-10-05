"""validate_image execution fencing. Lock owner -> upload -> image -> task.

All entry points require a caller-owned short transaction. Broker and object I/O
must remain outside it. Unsupported task types never acquire execution leases.
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from packages.domain.image_validation import ValidatedImage, validate_result
from packages.domain.job_execution import (
    TECHNICAL_ERRORS,
    ImageInput,
    ImageLease,
    InvalidDispatch,
    LeaseLost,
    retry_delay,
)
from packages.domain.security import ServiceError
from packages.domain.uploads import (
    CAPTURE_STATES,
    EVIDENCE_STATES,
    MAX_BYTES,
    MIME_TYPES,
    digest,
    identifier,
    owner_reference,
    staging_key,
)
from packages.domain.workflow import Item
from packages.persistence.dispatch import db_now, decoded, validate_dispatch
from packages.persistence.transaction import require_transaction
from packages.persistence.uploads import UploadRepository


def _row(connection, table, tenant, row_id, *, lock=False):
    # Identifiers are internal constants, never accepted from a message.
    return (
        connection.execute(
            text(
                f"SELECT * FROM {table} WHERE tenant_id=:tenant AND id=:id"
                + (" FOR UPDATE" if lock else "")
            ),
            {"tenant": tenant, "id": row_id},
        )
        .mappings()
        .first()
    )


def _load(connection, tenant, task_id):
    require_transaction(connection)
    tenant_status = connection.scalar(
        text("SELECT status FROM tenants WHERE id=:tenant FOR SHARE"), {"tenant": tenant}
    )
    locator = _row(connection, "task_runs", tenant, task_id)
    if locator is None or locator["task_type"] != "validate_image":
        return None, None
    source = _row(connection, "asset_images", tenant, locator["resource_id"])
    current = None
    if source is not None:
        owner_type, owner_id = owner_reference(source)
        try:
            owner = UploadRepository().owner(
                connection, SimpleNamespace(tenant_id=tenant), owner_type, owner_id
            )
        except ServiceError as error:
            if error.status != 404:
                raise
            owner = None
        upload = _row(connection, "uploads", tenant, source["upload_id"], lock=True)
        image = _row(connection, "asset_images", tenant, source["id"], lock=True)
        owner_valid = owner is not None and (
            (
                isinstance(owner, Item)
                and owner.status in CAPTURE_STATES
                and owner.inspection_status in {"draft", "in_progress"}
            )
            or (not isinstance(owner, Item) and owner.status in EVIDENCE_STATES)
        )
        if (
            tenant_status == "active"
            and owner_valid
            and upload is not None
            and image is not None
            and owner_reference(image) == (owner_type, owner_id)
            and owner_reference(upload) == (owner_type, owner_id)
            and image["upload_id"] == upload["id"]
            and image["laboratory_id"] == upload["laboratory_id"] == owner.laboratory_id
            and image["status"] == upload["status"] == "validating"
            and image["original_key"] == upload["object_key"] == staging_key(tenant, upload["id"])
            and image["original_sha256"] == upload["expected_sha256"]
            and image["mime_type"] == upload["mime_type"] in MIME_TYPES
            and 1 <= upload["size_bytes"] <= MAX_BYTES
            and image["original_object_version"] not in {"", "null"}
        ):
            digest(upload["expected_sha256"])
            current = ImageInput(
                image["id"],
                upload["id"],
                image["laboratory_id"],
                owner_type,
                owner_id,
                image["original_key"],
                image["original_object_version"],
                upload["expected_sha256"],
                upload["size_bytes"],
                upload["mime_type"],
                image["version"],
                upload["version"],
            )
    task = _row(connection, "task_runs", tenant, task_id, lock=True)
    if task is None or task["task_type"] != "validate_image":
        return None, None
    payload = decoded(task["payload"])
    if (
        current is None
        or task["resource_id"] != current.image_id
        or payload != {"image_id": current.image_id, "upload_id": current.upload_id}
        or task["logical_key"] != f"validate_image:{current.image_id}"
    ):
        current = None
    return task, current


def _attempt(connection, task, status, code, now):
    count = connection.execute(
        text(
            "UPDATE task_attempts SET status=:status,error_code=:code,finished_at=:now,"
            "updated_at=:now,version=version+1 WHERE tenant_id=:tenant AND task_id=:id "
            "AND replay_generation=:generation AND attempt=:attempt AND fencing_token=:token "
            "AND lease_owner=:owner AND status='running'"
        ),
        {
            "tenant": task["tenant_id"],
            "id": task["id"],
            "generation": task["replay_generation"],
            "attempt": task["attempt"],
            "token": task["fencing_token"],
            "owner": task["lease_owner"],
            "status": status,
            "code": code,
            "now": now,
        },
    ).rowcount
    if count != 1:
        raise RuntimeError("Missing current task attempt")


def _end(connection, task, *, state, code, now, available=None, abandoned=False):
    if task["state"] == "leased":
        _attempt(connection, task, "abandoned" if abandoned else "failed", code, now)
    connection.execute(
        text(
            "UPDATE task_runs SET state=:state,last_error_code=:code,lease_owner=NULL,"
            "lease_until=NULL,heartbeat_at=NULL,fencing_token=fencing_token+1,"
            "available_at=:available,finished_at=:finished,updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {
            "tenant": task["tenant_id"],
            "id": task["id"],
            "state": state,
            "code": code,
            "available": available or now,
            "finished": None if state == "retry_wait" else now,
            "now": now,
        },
    )


def _settle(connection, task, current, now, code, *, abandoned=False, delay=retry_delay):
    if current is None:
        state, code = "failed", "LEASE_LOST"
    elif TECHNICAL_ERRORS[code]:
        state = "retry_wait" if task["attempt"] < 4 else "dead_letter"
    else:
        state = "failed"
    _end(
        connection,
        task,
        state=state,
        code=code,
        now=now,
        abandoned=abandoned,
        available=now + delay(task["attempt"]) if state == "retry_wait" else None,
    )
    return state


def claim(connection, message, owner):
    validate_dispatch(message)
    identifier(owner)
    task, current = _load(connection, message["tenant_id"], message["task_id"])
    if task is None:
        return None
    # Generation dominates sequence: any prior generation is stale even if its
    # sequence is numerically higher; a future generation is always invalid.
    generation, sequence = message["replay_generation"], message["dispatch_sequence"]
    if generation < task["replay_generation"]:
        return None
    if generation > task["replay_generation"]:
        raise InvalidDispatch()
    if sequence < task["dispatch_sequence"]:
        return None
    if sequence > task["dispatch_sequence"]:
        raise InvalidDispatch()
    if message["resource_id"] != task["resource_id"] or message["payload"] != decoded(
        task["payload"]
    ):
        raise InvalidDispatch()
    now = db_now(connection)
    if task["state"] not in {"ready", "retry_wait"} or task["available_at"] > now:
        return None
    if current is None:
        _end(connection, task, state="failed", code="LEASE_LOST", now=now)
        return None
    if task["attempt"] >= 4:
        _end(connection, task, state="dead_letter", code="STAGE_TIMEOUT", now=now)
        return None
    attempt, token = task["attempt"] + 1, task["fencing_token"] + 1
    connection.execute(
        text(
            "UPDATE task_runs SET state='leased',attempt=:attempt,fencing_token=:token,"
            "lease_owner=:owner,lease_until=:until,heartbeat_at=:now,"
            "started_at=COALESCE(started_at,:now),finished_at=NULL,last_error_code=NULL,"
            "updated_at=:now,version=version+1 WHERE tenant_id=:tenant AND id=:id"
        ),
        {
            "tenant": task["tenant_id"],
            "id": task["id"],
            "attempt": attempt,
            "token": token,
            "owner": owner,
            "until": now + timedelta(seconds=60),
            "now": now,
        },
    )
    connection.execute(
        text(
            "INSERT INTO task_attempts (id,tenant_id,task_id,replay_generation,attempt,"
            "fencing_token,lease_owner,started_at,status,created_at,updated_at) VALUES "
            "(:id,:tenant,:task,:generation,:attempt,:token,:owner,:now,'running',:now,:now)"
        ),
        {
            "id": str(uuid4()),
            "tenant": task["tenant_id"],
            "task": task["id"],
            "generation": task["replay_generation"],
            "attempt": attempt,
            "token": token,
            "owner": owner,
            "now": now,
        },
    )
    return ImageLease(
        task["tenant_id"], task["id"], owner, token, task["replay_generation"], attempt, current
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


def heartbeat(connection, lease):
    task, current = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now):
        return False
    if current != lease.input:
        _settle(connection, task, None, now, "INTERNAL_ERROR")
        return False
    connection.execute(
        text(
            "UPDATE task_runs SET lease_until=:until,heartbeat_at=:now,updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {
            "tenant": lease.tenant_id,
            "id": lease.task_id,
            "until": now + timedelta(seconds=60),
            "now": now,
        },
    )
    return True


def fail(connection, lease, code, *, delay=retry_delay):
    if code not in TECHNICAL_ERRORS:
        raise ValueError("Use a validation outcome for content errors")
    task, current = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now):
        return False
    _settle(connection, task, current if current == lease.input else None, now, code, delay=delay)
    return True


def commit_result(connection, lease, write_result):
    """Server-owned handler writes image/upload/owner/audit/event in this transaction.

    No public route or fixture supplies this callback. Raising rolls back all
    handler writes and leaves the leased task for failure handling or recovery.
    """
    task, current = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now) or current != lease.input:
        raise LeaseLost()
    write_result(connection, lease.input)
    # A callback cannot manufacture succeeded without a terminal image result.
    image = _row(connection, "asset_images", lease.tenant_id, current.image_id, lock=True)
    upload = _row(connection, "uploads", lease.tenant_id, current.upload_id, lock=True)
    if image["status"] not in {"ready", "rejected"} or upload["status"] != image["status"]:
        raise RuntimeError("Handler did not persist a validation outcome")
    if image["status"] == "ready":
        if image["original_sha256"] != current.expected_sha256:
            raise ValueError("Ready result changed original digest")
        validate_result(
            lease.tenant_id,
            current,
            ValidatedImage(
                image["original_key"],
                image["original_object_version"],
                image["analysis_key"],
                image["analysis_object_version"],
                image["analysis_sha256"],
                image["width"],
                image["height"],
            ),
        )
    now = db_now(connection)
    if not _owned(
        _row(connection, "task_runs", lease.tenant_id, lease.task_id, lock=True), lease, now
    ):
        raise LeaseLost()
    _attempt(connection, task, "succeeded", None, now)
    connection.execute(
        text(
            "UPDATE task_runs SET state='succeeded',lease_owner=NULL,lease_until=NULL,"
            "heartbeat_at=NULL,finished_at=:now,last_error_code=NULL,updated_at=:now,"
            "version=version+1 WHERE tenant_id=:tenant AND id=:id"
        ),
        {"tenant": lease.tenant_id, "id": lease.task_id, "now": now},
    )


def expired_candidates(connection, limit=100):
    require_transaction(connection)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Batch limit must be 1..100")
    # Discovery only. Never lock task before the domain aggregate.
    return [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT tenant_id,id,fencing_token,replay_generation FROM task_runs "
                "WHERE task_type='validate_image' AND state='leased' "
                "AND lease_until<=UTC_TIMESTAMP(3) ORDER BY lease_until,id LIMIT :limit"
            ),
            {"limit": limit},
        ).mappings()
    ]


def recover(connection, candidate, *, delay=retry_delay):
    task, current = _load(connection, candidate["tenant_id"], candidate["id"])
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
    _settle(connection, task, current, now, "STAGE_TIMEOUT", abandoned=True, delay=delay)
    return True
