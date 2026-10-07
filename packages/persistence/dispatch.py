"""Short MySQL transactions for I-03A1 validate_image delivery only.

No broker I/O belongs here. Task execution leases and domain transitions are
deliberately separate from publisher leases.
Callers use READ COMMITTED scheduler connections, not the API's RR snapshots.
"""

import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from packages.domain.uploads import capture_time, identifier
from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json


def validate_dispatch(message):
    """Fail closed on the implemented task-message-v1 branches (schema 1.1)."""
    import re

    fields = {
        "schema_version",
        "tenant_id",
        "trace_id",
        "task_id",
        "task_type",
        "resource_id",
        "replay_generation",
        "dispatch_sequence",
        "created_at",
        "payload",
    }
    if not isinstance(message, dict) or set(message) != fields:
        raise ValueError("Invalid dispatch envelope")
    if message["schema_version"] != "1.1" or message["task_type"] not in {
        "validate_image",
        "inference_pipeline",
        "rule_evaluation",
        "report_export",
    }:
        raise ValueError("Unsupported dispatch protocol or task type")
    for name in ("tenant_id", "task_id", "resource_id"):
        identifier(message[name])
    trace = message["trace_id"]
    if not isinstance(trace, str) or re.fullmatch("[a-f0-9]{32}", trace) is None:
        raise ValueError("Invalid trace")
    for name, lower in (("replay_generation", 0), ("dispatch_sequence", 1)):
        value = message[name]
        if type(value) is not int or not lower <= value <= 2147483647:
            raise ValueError("Invalid dispatch counter")
    capture_time(message["created_at"])
    payload = message["payload"]
    if not isinstance(payload, dict):
        raise ValueError("Invalid task payload")
    if message["task_type"] == "validate_image":
        if set(payload) != {"upload_id", "image_id"}:
            raise ValueError("Invalid validate_image payload")
        identifier(payload["upload_id"])
        identifier(payload["image_id"])
        if payload["image_id"] != message["resource_id"]:
            raise ValueError("Inconsistent dispatch resource")
    elif message["task_type"] == "inference_pipeline":
        if set(payload) != {
            "run_id",
            "submission_revision",
            "model_bundle_id",
            "dictionary_version_id",
            "rule_bundle_id",
        }:
            raise ValueError("Invalid inference_run payload")
        for name in ("run_id", "model_bundle_id", "dictionary_version_id", "rule_bundle_id"):
            identifier(payload[name])
        if payload["run_id"] != message["resource_id"]:
            raise ValueError("Inconsistent inference resource")
        if (
            type(payload["submission_revision"]) is not int
            or not 1 <= payload["submission_revision"] <= 2147483647
        ):
            raise ValueError("Invalid inference revision")
    elif message["task_type"] == "rule_evaluation":
        if set(payload) != {"run_id", "fact_revision_id", "rule_bundle_id"}:
            raise ValueError("Invalid rule_evaluation payload")
        for name in ("run_id", "fact_revision_id", "rule_bundle_id"):
            identifier(payload[name])
        if payload["run_id"] != message["resource_id"]:
            raise ValueError("Inconsistent rule evaluation resource")
    else:
        if set(payload) != {"export_id"} or payload["export_id"] != message["resource_id"]:
            raise ValueError("Invalid report_export payload")
        identifier(payload["export_id"])
    return message


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def db_now(connection):
    require_transaction(connection)
    return connection.scalar(text("SELECT UTC_TIMESTAMP(3)"))


def _supported(alias, report_enabled, *, outbox=False):
    if type(report_enabled) is not bool:
        raise ValueError("Report dispatch flag must be boolean")
    kind = (
        f"JSON_UNQUOTE(JSON_EXTRACT({alias}.payload,'$.task_type'))"
        if outbox
        else f"{alias}.task_type"
    )
    existing = f"{kind} IN ('validate_image','inference_pipeline','rule_evaluation')"
    if not report_enabled:
        return existing
    resource = (
        f"JSON_UNQUOTE(JSON_EXTRACT({alias}.payload,'$.resource_id'))"
        if outbox
        else f"{alias}.resource_id"
    )
    return (
        f"({existing} OR ({kind}='report_export' AND EXISTS ("
        f"SELECT 1 FROM report_exports e WHERE e.tenant_id={alias}.tenant_id "
        f"AND e.id={resource} AND e.format='csv')))"
    )


def claim(connection, owner, *, report_enabled=False):
    """Claim just one row so waiting behind slow publishes cannot expire a batch."""
    identifier(owner)
    db_now(connection)
    row = (
        connection.execute(
            text(
                "SELECT o.* FROM outbox_events o WHERE event_type='TaskDispatch' AND "
                + _supported("o", report_enabled, outbox=True)
                + " "
                "AND state='pending' AND available_at<=UTC_TIMESTAMP(3) "
                "ORDER BY available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    now = db_now(connection)
    connection.execute(
        text(
            "UPDATE outbox_events SET state='leased',lease_owner=:owner,lease_until=:until,"
            "attempts=attempts+1,updated_at=:now,version=version+1 WHERE id=:id"
        ),
        {"owner": owner, "until": now + timedelta(seconds=30), "now": now, "id": row["id"]},
    )
    return dict(row)


def owned_update(connection, row, owner, operation):
    """Lock before reading the database clock, preventing stale-time acceptance."""
    require_transaction(connection)
    current = (
        connection.execute(
            text(
                "SELECT state,lease_owner,lease_until FROM outbox_events "
                "WHERE tenant_id=:tenant AND id=:id FOR UPDATE"
            ),
            {"tenant": row["tenant_id"], "id": row["id"]},
        )
        .mappings()
        .first()
    )
    now = db_now(connection)
    if (
        current is None
        or current["state"] != "leased"
        or current["lease_owner"] != owner
        or current["lease_until"] is None
        or current["lease_until"] <= now
    ):
        return False
    updates = {
        "renew": "lease_until=:until",
        "published": "state='published',published_at=:now,lease_owner=NULL,lease_until=NULL",
        "release": "state='pending',available_at=:retry,lease_owner=NULL,lease_until=NULL",
    }
    connection.execute(
        text(
            f"UPDATE outbox_events SET {updates[operation]},updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {
            "tenant": row["tenant_id"],
            "id": row["id"],
            "now": now,
            "until": now + timedelta(seconds=30),
            "retry": now + timedelta(seconds=30),
        },
    )
    return True


def recover_publications(connection, limit=100, *, report_enabled=False):
    require_transaction(connection)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Batch limit must be 1..100")
    rows = (
        connection.execute(
            text(
                "SELECT o.id FROM outbox_events o WHERE event_type='TaskDispatch' AND "
                + _supported("o", report_enabled, outbox=True)
                + " "
                "AND state='leased' AND lease_until<=UTC_TIMESTAMP(3) "
                "ORDER BY lease_until,id LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    now = db_now(connection)
    for row in rows:
        connection.execute(
            text(
                "UPDATE outbox_events SET state='pending',lease_owner=NULL,lease_until=NULL,"
                "available_at=:now,updated_at=:now,version=version+1 WHERE id=:id"
            ),
            {"id": row["id"], "now": now},
        )
    return len(rows)


def redispatch(connection, limit=100, *, report_enabled=False):
    """Rebuild notices from DB payload, even if the initial event was published.

    last_dispatched_at is the time an intent was registered, not a broker ack.
    It is changed atomically with the sequence and new outbox row.
    """
    require_transaction(connection)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Batch limit must be 1..100")
    rows = (
        connection.execute(
            text(
                "SELECT t.* FROM task_runs t WHERE " + _supported("t", report_enabled) + " "
                "AND state IN ('ready','retry_wait') AND available_at<=UTC_TIMESTAMP(3) "
                "AND (last_dispatched_at IS NULL OR "
                "last_dispatched_at<=UTC_TIMESTAMP(3)-INTERVAL 30 SECOND) "
                "ORDER BY available_at,id LIMIT :limit FOR UPDATE SKIP LOCKED"
            ),
            {"limit": limit},
        )
        .mappings()
        .all()
    )
    now = db_now(connection)
    for row in rows:
        sequence = row["dispatch_sequence"] + 1
        message = validate_dispatch(
            {
                "schema_version": "1.1",
                "tenant_id": row["tenant_id"],
                "trace_id": uuid4().hex,
                "task_id": row["id"],
                "task_type": row["task_type"],
                "resource_id": row["resource_id"],
                "replay_generation": row["replay_generation"],
                "dispatch_sequence": sequence,
                "created_at": timestamp(now),
                "payload": decoded(row["payload"]),
            }
        )
        connection.execute(
            text(
                "UPDATE task_runs SET dispatch_sequence=:sequence,last_dispatched_at=:now,"
                "updated_at=:now,version=version+1 WHERE tenant_id=:tenant AND id=:id"
            ),
            {"sequence": sequence, "now": now, "tenant": row["tenant_id"], "id": row["id"]},
        )
        connection.execute(
            text(
                "INSERT INTO outbox_events (id,tenant_id,event_type,aggregate_type,aggregate_id,"
                "aggregate_version,payload,state,available_at,created_at,updated_at) VALUES "
                "(:id,:tenant,'TaskDispatch','task_run',:task,:version,:payload,'pending',"
                ":now,:now,:now)"
            ),
            {
                "id": str(uuid4()),
                "tenant": row["tenant_id"],
                "task": row["id"],
                "version": row["version"] + 1,
                "payload": canonical_json(message),
                "now": now,
            },
        )
    return len(rows)
