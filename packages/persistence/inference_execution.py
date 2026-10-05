"""Inference task lease and result persistence.

The lock order is item -> current run -> task.  AI calls happen outside these
short transactions; only the final fenced write is performed while locked.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from packages.domain.inference_execution import (
    RETRYABLE_ERRORS,
    InferenceImage,
    InferenceInput,
    InferenceInvalidResult,
    InferenceLease,
    InferenceLeaseLost,
    validate_result,
)
from packages.domain.job_execution import retry_delay
from packages.persistence.dispatch import db_now, decoded, validate_dispatch
from packages.persistence.transaction import require_transaction
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json, sha256_json


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
    task = _task(connection, tenant, task_id)
    if task is None or task["task_type"] != "inference_pipeline":
        return None, None
    tenant_status = connection.scalar(
        text("SELECT status FROM tenants WHERE id=:tenant FOR SHARE"), {"tenant": tenant}
    )
    if tenant_status != "active":
        return task, None
    # Domain-first lock order: tenant -> item -> current run -> task.
    run = (
        connection.execute(
            text("SELECT * FROM inference_runs WHERE tenant_id=:tenant AND id=:run"),
            {"tenant": tenant, "run": task["resource_id"]},
        )
        .mappings()
        .first()
    )
    if run is None:
        return None, None
    item = (
        connection.execute(
            text("SELECT * FROM inspection_items WHERE tenant_id=:tenant AND id=:item FOR UPDATE"),
            {"tenant": tenant, "item": run["item_id"]},
        )
        .mappings()
        .first()
    )
    run = (
        connection.execute(
            text("SELECT * FROM inference_runs WHERE tenant_id=:tenant AND id=:run FOR UPDATE"),
            {"tenant": tenant, "run": task["resource_id"]},
        )
        .mappings()
        .first()
    )
    task = _task(connection, tenant, task_id, lock=True)
    if (
        item is None
        or item["current_run_id"] != run["id"]
        or run["status"] in {"superseded", "completed", "needs_retake", "needs_review", "failed"}
    ):
        return task, None
    payload = decoded(task["payload"])
    model = (
        connection.execute(
            text(
                "SELECT m.checksum AS model_checksum,d.checksum AS dictionary_sha256 "
                "FROM model_versions m JOIN dictionary_versions d "
                "ON d.tenant_id=m.tenant_id AND d.id=m.dictionary_version_id "
                "WHERE m.tenant_id=:tenant AND m.id=:model AND d.id=:dictionary"
            ),
            {
                "tenant": tenant,
                "model": run["model_bundle_id"],
                "dictionary": run["dictionary_version_id"],
            },
        )
        .mappings()
        .first()
    )
    image_rows = (
        connection.execute(
            text(
                "SELECT ri.image_id,ri.role,ri.parent_image_id,ri.analysis_sha256,"
                "ai.analysis_sha256 AS asset_analysis_sha256,ai.analysis_key,"
                "ai.analysis_object_version,ai.mime_type,ai.status AS image_status,i.location_id "
                "FROM run_images ri JOIN asset_images ai ON ai.tenant_id=ri.tenant_id "
                "AND ai.inspection_item_id=ri.item_id AND ai.id=ri.image_id "
                "JOIN inspection_items i ON i.tenant_id=ri.tenant_id AND i.id=ri.item_id "
                "WHERE ri.tenant_id=:tenant AND ri.item_id=:item AND ri.run_id=:run "
                "ORDER BY ri.ordinal FOR UPDATE"
            ),
            {"tenant": tenant, "item": run["item_id"], "run": run["id"]},
        )
        .mappings()
        .all()
    )
    if model is None or not image_rows:
        return task, None
    if any(
        row["analysis_key"] is None
        or row["analysis_sha256"] != row["asset_analysis_sha256"]
        or row["analysis_object_version"] in (None, "", "null")
        or row["analysis_sha256"] is None
        or row["mime_type"] != "image/png"
        or row["image_status"] != "ready"
        for row in image_rows
    ):
        return task, None
    images = tuple(
        InferenceImage(
            image_id=row["image_id"],
            object_key=row["analysis_key"],
            sha256=row["analysis_sha256"],
            mime_type=row["mime_type"],
            role=row["role"],
            location_id=item["location_id"],
            parent_image_id=row["parent_image_id"],
        )
        for row in image_rows
    )
    expected = {
        "run_id": run["id"],
        "submission_revision": run["submission_revision"],
        "model_bundle_id": run["model_bundle_id"],
        "dictionary_version_id": run["dictionary_version_id"],
        "rule_bundle_id": run["rule_bundle_id"],
    }
    if payload != expected or task["resource_id"] != run["id"]:
        return task, None
    return task, InferenceInput(
        tenant_id=run["tenant_id"],
        laboratory_id=run["laboratory_id"],
        item_id=run["item_id"],
        run_id=run["id"],
        submission_revision=run["submission_revision"],
        model_bundle_id=run["model_bundle_id"],
        model_checksum=model["model_checksum"],
        dictionary_version_id=run["dictionary_version_id"],
        dictionary_sha256=model["dictionary_sha256"],
        pipeline_version=run["pipeline_version"],
        device_profile=run["device_profile"],
        images=images,
    )


def _attempt(connection, task, attempt_id, status, code, now):
    changed = connection.execute(
        text(
            "UPDATE task_attempts SET status=:status,error_code=:code,finished_at=:now,"
            "updated_at=:now,version=version+1 WHERE tenant_id=:tenant AND id=:id "
            "AND status='running' AND task_id=:task AND replay_generation=:generation "
            "AND attempt=:attempt AND fencing_token=:token AND lease_owner=:owner"
        ),
        {
            "tenant": task["tenant_id"],
            "id": attempt_id,
            "task": task["id"],
            "generation": task["replay_generation"],
            "attempt": task["attempt"],
            "token": task["fencing_token"],
            "owner": task["lease_owner"],
            "status": status,
            "code": code,
            "now": now,
        },
    ).rowcount
    if changed != 1:
        raise RuntimeError("Missing current inference attempt")


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


def _settle(connection, task, lease, now, code, *, abandoned=False):
    retry = code in RETRYABLE_ERRORS and task["attempt"] < 4
    state = "retry_wait" if retry else ("dead_letter" if code in RETRYABLE_ERRORS else "failed")
    _attempt(connection, task, lease.attempt_id, "abandoned" if abandoned else "failed", code, now)
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
            "available": now + retry_delay(task["attempt"]) if retry else now,
            "finished": None if retry else now,
            "now": now,
        },
    )
    connection.execute(
        text(
            "UPDATE inference_runs SET status=:status,error_code=:code,updated_at=:now,"
            "finished_at=:finished,version=version+1 WHERE tenant_id=:tenant AND id=:run"
        ),
        {
            "status": "retrying" if retry else "failed",
            "code": code,
            "finished": None if retry else now,
            "now": now,
            "tenant": lease.tenant_id,
            "run": lease.input.run_id,
        },
    )
    if not retry:
        connection.execute(
            text(
                "UPDATE inspection_items SET status='failed',version=version+1,updated_at=:now "
                "WHERE tenant_id=:tenant AND id=:item AND current_run_id=:run"
            ),
            {
                "tenant": lease.tenant_id,
                "item": lease.input.item_id,
                "run": lease.input.run_id,
                "now": now,
            },
        )
    return state


def claim(connection, message, owner):
    validate_dispatch(message)
    task, current = _load(connection, message["tenant_id"], message["task_id"])
    if task is None:
        return None
    if message["replay_generation"] != task["replay_generation"]:
        if message["replay_generation"] > task["replay_generation"]:
            raise ValueError("future generation")
        return None
    if message["dispatch_sequence"] != task["dispatch_sequence"]:
        if message["dispatch_sequence"] > task["dispatch_sequence"]:
            raise ValueError("future sequence")
        return None
    if message["resource_id"] != task["resource_id"] or message["payload"] != decoded(
        task["payload"]
    ):
        raise ValueError("Invalid inference dispatch")
    now = db_now(connection)
    if task["state"] not in {"ready", "retry_wait"} or task["available_at"] > now:
        return None
    if current is None:
        connection.execute(
            text(
                "UPDATE task_runs SET state='failed',last_error_code='LEASE_LOST',"
                "finished_at=:now,updated_at=:now,version=version+1 "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"now": now, "tenant": task["tenant_id"], "id": task["id"]},
        )
        return None
    if task["attempt"] >= 4:
        connection.execute(
            text(
                "UPDATE task_runs SET state='dead_letter',last_error_code='STAGE_TIMEOUT',"
                "finished_at=:now,updated_at=:now,version=version+1 "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"now": now, "tenant": task["tenant_id"], "id": task["id"]},
        )
        connection.execute(
            text(
                "UPDATE inference_runs SET status='failed',error_code='STAGE_TIMEOUT',"
                "finished_at=:now,updated_at=:now,version=version+1 "
                "WHERE tenant_id=:tenant AND id=:run"
            ),
            {"now": now, "tenant": task["tenant_id"], "run": task["resource_id"]},
        )
        return None
    attempt, token, attempt_id = task["attempt"] + 1, task["fencing_token"] + 1, str(uuid4())
    connection.execute(
        text(
            "UPDATE task_runs SET state='leased',attempt=:attempt,fencing_token=:token,"
            "lease_owner=:owner,lease_until=:until,heartbeat_at=:now,started_at=COALESCE(started_at,:now),"
            "finished_at=NULL,last_error_code=NULL,updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {
            "attempt": attempt,
            "token": token,
            "owner": owner,
            "until": now + timedelta(seconds=60),
            "now": now,
            "tenant": task["tenant_id"],
            "id": task["id"],
        },
    )
    connection.execute(
        text(
            "INSERT INTO task_attempts "
            "(id,tenant_id,task_id,replay_generation,attempt,fencing_token,lease_owner,"
            "started_at,status,created_at,updated_at) "
            "VALUES (:id,:tenant,:task,:generation,:attempt,:token,:owner,:now,'running',:now,:now)"
        ),
        {
            "id": attempt_id,
            "tenant": task["tenant_id"],
            "task": task["id"],
            "generation": task["replay_generation"],
            "attempt": attempt,
            "token": token,
            "owner": owner,
            "now": now,
        },
    )
    connection.execute(
        text(
            "UPDATE inference_runs SET status='processing',stage='quality',"
            "started_at=COALESCE(started_at,:now),updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:run"
        ),
        {"tenant": task["tenant_id"], "run": task["resource_id"], "now": now},
    )
    connection.execute(
        text(
            "UPDATE inspection_items SET status='quality_checking',version=version+1,"
            "updated_at=:now WHERE tenant_id=:tenant AND id=:item AND current_run_id=:run"
        ),
        {"tenant": task["tenant_id"], "item": current.item_id, "run": current.run_id, "now": now},
    )
    return InferenceLease(
        task["tenant_id"],
        task["id"],
        owner,
        attempt_id,
        token,
        task["replay_generation"],
        attempt,
        current,
    )


def heartbeat(connection, lease):
    task, current = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now) or current != lease.input:
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
    task, current = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now) or current != lease.input:
        return False
    _settle(connection, task, lease, now, code)
    return True


def commit_result(connection, lease, result):
    validate_result(result, lease)
    task, current = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now) or current != lease.input:
        raise InferenceLeaseLost()
    outcome = result["outcome"]
    if outcome == "needs_retake":
        run_status, run_stage, item_status = "needs_retake", "done", "needs_retake"
    elif outcome == "needs_review":
        run_status, run_stage, item_status = "needs_review", "done", "needs_review"
    elif outcome == "facts_ready":
        run_status, run_stage, item_status = "processing", "rules", "processing"
    else:
        raise InferenceInvalidResult()
    fact_id = None
    evaluation_id = None
    rule_task_id = None
    if outcome == "facts_ready":
        fact_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO fact_revisions "
                "(id,tenant_id,item_id,run_id,revision,entities,relations,dates,reason) "
                "VALUES (:id,:tenant,:item,:run,1,:entities,:relations,:dates,:reason)"
            ),
            {
                "id": fact_id,
                "tenant": lease.tenant_id,
                "item": lease.input.item_id,
                "run": lease.input.run_id,
                "entities": canonical_json(result["entities"]),
                "relations": canonical_json(result["relations"]),
                "dates": "[]",
                "reason": "AI inference facts",
            },
        )
        evaluation_id = str(uuid4())
        rule_bundle_id = connection.scalar(
            text("SELECT rule_bundle_id FROM inference_runs WHERE tenant_id=:tenant AND id=:run"),
            {"tenant": lease.tenant_id, "run": lease.input.run_id},
        )
        connection.execute(
            text(
                "INSERT INTO rule_evaluations "
                "(id,tenant_id,item_id,run_id,fact_revision_id,rule_bundle_id,"
                "reference_date,status) "
                "SELECT :id,tenant_id,item_id,id,:fact,rule_bundle_id,reference_date,'queued' "
                "FROM inference_runs WHERE tenant_id=:tenant AND id=:run"
            ),
            {
                "id": evaluation_id,
                "fact": fact_id,
                "tenant": lease.tenant_id,
                "run": lease.input.run_id,
            },
        )
        rule_task_id = str(uuid4())
        rule_payload = {
            "run_id": lease.input.run_id,
            "fact_revision_id": fact_id,
            "rule_bundle_id": rule_bundle_id,
        }
        connection.execute(
            text(
                "INSERT INTO task_runs "
                "(id,tenant_id,task_type,resource_id,logical_key,payload,state,attempt,"
                "replay_generation,dispatch_sequence,fencing_token,available_at,created_at,"
                "updated_at) "
                "VALUES (:id,:tenant,'rule_evaluation',:run,:logical,:payload,'ready',0,0,1,0,"
                ":now,:now,:now)"
            ),
            {
                "id": rule_task_id,
                "tenant": lease.tenant_id,
                "run": lease.input.run_id,
                "logical": f"rule_evaluation:{evaluation_id}",
                "payload": canonical_json(rule_payload),
                "now": now,
            },
        )
        event = {
            "schema_version": "1.1",
            "tenant_id": lease.tenant_id,
            "trace_id": uuid4().hex,
            "task_id": rule_task_id,
            "task_type": "rule_evaluation",
            "resource_id": lease.input.run_id,
            "replay_generation": 0,
            "dispatch_sequence": 1,
            "created_at": timestamp(now),
            "payload": rule_payload,
        }
        connection.execute(
            text(
                "INSERT INTO outbox_events "
                "(id,tenant_id,event_type,aggregate_type,aggregate_id,aggregate_version,"
                "payload,state,available_at,created_at,updated_at) "
                "VALUES (:id,:tenant,'TaskDispatch','task_run',:task,1,:payload,'pending',"
                ":now,:now,:now)"
            ),
            {
                "id": str(uuid4()),
                "tenant": lease.tenant_id,
                "task": rule_task_id,
                "payload": canonical_json(event),
                "now": now,
            },
        )
    connection.execute(
        text(
            "UPDATE inference_runs SET status=:status,stage=:stage,result=:result,"
            "result_hash=:hash,finished_at=:finished,updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:run"
        ),
        {
            "status": run_status,
            "stage": run_stage,
            "result": canonical_json(result),
            "hash": sha256_json(result),
            "now": now,
            "finished": now if run_stage == "done" else None,
            "tenant": lease.tenant_id,
            "run": lease.input.run_id,
        },
    )
    connection.execute(
        text(
            "UPDATE inspection_items SET status=:status,current_fact_revision_id=:fact,"
            "current_evaluation_id=:evaluation,"
            "version=version+1,updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:item AND current_run_id=:run"
        ),
        {
            "status": item_status,
            "fact": fact_id,
            "evaluation": evaluation_id,
            "now": now,
            "tenant": lease.tenant_id,
            "item": lease.input.item_id,
            "run": lease.input.run_id,
        },
    )
    _attempt(connection, task, lease.attempt_id, "succeeded", None, now)
    connection.execute(
        text(
            "UPDATE task_runs SET state='succeeded',lease_owner=NULL,lease_until=NULL,"
            "heartbeat_at=NULL,finished_at=:now,updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:id"
        ),
        {"now": now, "tenant": lease.tenant_id, "id": lease.task_id},
    )


def expired_candidates(connection, limit=100):
    require_transaction(connection)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Batch limit must be 1..100")
    return [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT tenant_id,id,fencing_token,replay_generation FROM task_runs "
                "WHERE task_type='inference_pipeline' AND state='leased' "
                "AND lease_until<=UTC_TIMESTAMP(3) ORDER BY lease_until,id LIMIT :limit"
            ),
            {"limit": limit},
        ).mappings()
    ]


def recover(connection, candidate):
    task, current = _load(connection, candidate["tenant_id"], candidate["id"])
    now = db_now(connection)
    if (
        task is None
        or current is None
        or task["state"] != "leased"
        or task["lease_until"] is None
        or task["lease_until"] > now
        or task["fencing_token"] != candidate["fencing_token"]
        or task["replay_generation"] != candidate["replay_generation"]
    ):
        return False
    lease = InferenceLease(
        task["tenant_id"],
        task["id"],
        task["lease_owner"],
        "",
        task["fencing_token"],
        task["replay_generation"],
        task["attempt"],
        current,
    )
    attempt = connection.execute(
        text(
            "SELECT id FROM task_attempts WHERE tenant_id=:tenant AND task_id=:task "
            "AND replay_generation=:generation AND attempt=:attempt"
        ),
        {
            "tenant": task["tenant_id"],
            "task": task["id"],
            "generation": task["replay_generation"],
            "attempt": task["attempt"],
        },
    ).scalar_one()
    lease = InferenceLease(
        lease.tenant_id,
        lease.task_id,
        lease.owner,
        attempt,
        lease.token,
        lease.generation,
        lease.attempt,
        lease.input,
    )
    _settle(connection, task, lease, now, "STAGE_TIMEOUT", abandoned=True)
    return True
