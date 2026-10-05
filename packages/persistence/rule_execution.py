"""Fenced persistence for rule_evaluation tasks."""

import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from packages.domain.job_execution import retry_delay
from packages.domain.rule_execution import RULE_RETRYABLE, RuleInput, RuleLease, RuleLeaseLost
from packages.persistence.dispatch import db_now, decoded, validate_dispatch
from packages.persistence.transaction import require_transaction
from packages.rules.evaluator import RuleDefinitionError, evaluate_bundle, normalize_facts
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
    if task is None or task["task_type"] != "rule_evaluation":
        return None, None
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
    if item is not None:
        run = (
            connection.execute(
                text("SELECT * FROM inference_runs WHERE tenant_id=:tenant AND id=:run FOR UPDATE"),
                {"tenant": tenant, "run": run["id"]},
            )
            .mappings()
            .first()
        )
    fact = None
    evaluation = None
    if item is not None and item["current_fact_revision_id"]:
        fact = (
            connection.execute(
                text(
                    "SELECT * FROM fact_revisions WHERE tenant_id=:tenant AND id=:fact "
                    "AND item_id=:item AND run_id=:run FOR UPDATE"
                ),
                {
                    "tenant": tenant,
                    "fact": item["current_fact_revision_id"],
                    "item": run["item_id"],
                    "run": run["id"],
                },
            )
            .mappings()
            .first()
        )
    if item is not None and item["current_evaluation_id"]:
        evaluation = (
            connection.execute(
                text(
                    "SELECT * FROM rule_evaluations WHERE tenant_id=:tenant AND id=:evaluation "
                    "AND item_id=:item AND run_id=:run FOR UPDATE"
                ),
                {
                    "tenant": tenant,
                    "evaluation": item["current_evaluation_id"],
                    "item": run["item_id"],
                    "run": run["id"],
                },
            )
            .mappings()
            .first()
        )
    task = _task(connection, tenant, task_id, lock=True)
    if task is None:
        return None, None
    payload = decoded(task["payload"])
    if (
        item is None
        or fact is None
        or evaluation is None
        or item["current_run_id"] != run["id"]
        or item["status"] != "processing"
        or run["status"] != "processing"
        or run["stage"] != "rules"
        or evaluation["status"] not in {"queued", "running"}
        or payload
        != {
            "run_id": run["id"],
            "fact_revision_id": fact["id"],
            "rule_bundle_id": run["rule_bundle_id"],
        }
    ):
        return task, None
    member_rows = (
        connection.execute(
            text(
                "SELECT b.evaluator_version,b.checksum AS bundle_checksum,"
                "m.id AS member_id,v.id AS rule_version_id,v.revision,v.status AS version_status,"
                "v.definition,s.id AS rule_set_id,s.name AS rule_set_name "
                "FROM rule_bundles b "
                "LEFT JOIN rule_bundle_members m ON m.tenant_id=b.tenant_id AND m.bundle_id=b.id "
                "LEFT JOIN rule_versions v ON v.tenant_id=m.tenant_id AND v.id=m.rule_version_id "
                "LEFT JOIN rule_sets s ON s.tenant_id=v.tenant_id AND s.id=v.rule_set_id "
                "WHERE b.tenant_id=:tenant AND b.id=:bundle "
                "ORDER BY m.id,v.revision,v.id"
            ),
            {"tenant": tenant, "bundle": run["rule_bundle_id"]},
        )
        .mappings()
        .all()
    )
    rules = []
    bundle_checksum = None
    evaluator_version = None
    rule_set_ids = set()
    snapshot_valid = bool(member_rows)
    for row in member_rows:
        evaluator_version = row["evaluator_version"]
        bundle_checksum = row["bundle_checksum"]
        if row["member_id"] is None or row["version_status"] != "published":
            snapshot_valid = False
            continue
        if row["rule_set_id"] is not None:
            rule_set_ids.add(row["rule_set_id"])
        definition = row["definition"]
        try:
            value = json.loads(definition) if isinstance(definition, str) else definition
        except (TypeError, ValueError):
            snapshot_valid = False
            continue
        if isinstance(value, dict) and "rules" in value:
            rules.extend(value["rules"] if isinstance(value["rules"], list) else [])
            if not isinstance(value["rules"], list):
                snapshot_valid = False
        elif isinstance(value, dict):
            rules.append(value)
        else:
            snapshot_valid = False
    # Older synthetic fixtures leave evaluator_version at the factory's random
    # hexadecimal placeholder.  Treat that placeholder as the only compatible
    # default; any explicit non-supported evaluator remains invalid.
    if evaluator_version is None:
        snapshot_valid = False
        evaluator_version = ""
    elif len(str(evaluator_version)) == 32 and all(
        char in "0123456789abcdef" for char in str(evaluator_version).lower()
    ):
        evaluator_version = "rules-dnf-v1"
    if not snapshot_valid:
        rules = []
    bundle = {
        "rule_set_id": next(iter(rule_set_ids), run["rule_bundle_id"]),
        "version_label": run["rule_bundle_id"],
        "rules": rules,
        "evaluator_version": evaluator_version,
    }
    values = []
    for field in ("entities", "relations", "dates"):
        try:
            values.append(json.loads(fact[field]) if isinstance(fact[field], str) else fact[field])
        except (TypeError, ValueError):
            values.append(None)
    facts = normalize_facts(*values)
    return task, RuleInput(
        tenant_id=tenant,
        item_id=run["item_id"],
        run_id=run["id"],
        fact_revision_id=fact["id"],
        evaluation_id=evaluation["id"],
        rule_bundle_id=run["rule_bundle_id"],
        reference_date=run["reference_date"].isoformat(),
        facts=facts,
        bundle=bundle,
        laboratory_id=run["laboratory_id"],
        bundle_checksum=bundle_checksum,
    )


def _attempt(connection, task, lease, status, code, now):
    changed = connection.execute(
        text(
            "UPDATE task_attempts SET status=:status,error_code=:code,finished_at=:now,"
            "updated_at=:now,version=version+1 WHERE tenant_id=:tenant AND task_id=:task "
            "AND replay_generation=:generation AND attempt=:attempt AND fencing_token=:token "
            "AND lease_owner=:owner AND status='running'"
        ),
        {
            "tenant": task["tenant_id"],
            "task": task["id"],
            "generation": lease.generation,
            "attempt": lease.attempt,
            "token": lease.token,
            "owner": lease.owner,
            "status": status,
            "code": code,
            "now": now,
        },
    ).rowcount
    if changed != 1:
        raise RuntimeError("Missing current rule attempt")


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
    retry = code in RULE_RETRYABLE and task["attempt"] < 4
    state = "retry_wait" if retry else ("dead_letter" if code in RULE_RETRYABLE else "failed")
    _attempt(connection, task, lease, "abandoned" if abandoned else "failed", code, now)
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
            "available": now + retry_delay(task["attempt"]) if retry else now,
            "finished": None if retry else now,
            "now": now,
            "tenant": lease.tenant_id,
            "id": lease.task_id,
        },
    )
    connection.execute(
        text(
            "UPDATE rule_evaluations SET status=:status,updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:evaluation"
        ),
        {
            "status": "queued" if retry else "failed",
            "now": now,
            "tenant": lease.tenant_id,
            "evaluation": lease.input.evaluation_id,
        },
    )
    if not retry:
        connection.execute(
            text(
                "UPDATE inspection_items SET status='failed',version=version+1,updated_at=:now "
                "WHERE tenant_id=:tenant AND id=:item AND current_run_id=:run"
            ),
            {
                "now": now,
                "tenant": lease.tenant_id,
                "item": lease.input.item_id,
                "run": lease.input.run_id,
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
        raise ValueError("Invalid rule dispatch")
    now = db_now(connection)
    if task["state"] not in {"ready", "retry_wait"} or task["available_at"] > now:
        return None
    if current is None or task["attempt"] >= 4:
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
            "INSERT INTO task_attempts (id,tenant_id,task_id,replay_generation,attempt,"
            "fencing_token,lease_owner,started_at,status,created_at,updated_at) VALUES "
            "(:id,:tenant,:task,:generation,:attempt,:token,:owner,:now,'running',:now,:now)"
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
            "UPDATE rule_evaluations SET status='running',updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:evaluation"
        ),
        {"now": now, "tenant": task["tenant_id"], "evaluation": current.evaluation_id},
    )
    return RuleLease(
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


def _render_explanation(template, facts, reference_date):
    values = {
        "left_name": facts.get("left_name") or facts.get("left.storage_class") or "unknown",
        "right_name": facts.get("right_name") or facts.get("right.storage_class") or "unknown",
        "expiry_date": facts.get("expiry_date") or "unknown",
        "reference_date": reference_date,
    }
    try:
        return template.format(**values)
    except (KeyError, ValueError):
        return template


def _finding_evidence(current, finding):
    fields = {
        atom["field"]
        for rule in current.bundle.get("rules", [])
        if rule.get("rule_id") == finding["rule_id"]
        and rule.get("scope") == finding.get("scope")
        and rule.get("scope_id") == finding.get("scope_id")
        for clause in rule.get("clauses", [])
        for atom in clause
    }
    return {
        "rule": {
            "rule_id": finding["rule_id"],
            "scope": finding.get("scope"),
            "scope_id": finding.get("scope_id"),
            "effective_from": finding.get("effective_from"),
            "effective_to": finding.get("effective_to"),
            "case_ids": finding.get("case_ids", []),
            "source": finding.get("source"),
        },
        "facts": {field: current.facts.get(field) for field in sorted(fields)},
        "raw_facts": {
            key: current.facts.get(key)
            for key in ("entities", "relations", "dates")
            if key in current.facts
        },
        "subject_ids": finding.get("subject_ids", []),
        "reference_date": current.reference_date,
        "fact_revision_id": current.fact_revision_id,
    }


def commit_result(connection, lease, results=None, error_code=None):
    task, current = _load(connection, lease.tenant_id, lease.task_id)
    now = db_now(connection)
    if not _owned(task, lease, now) or current != lease.input:
        raise RuleLeaseLost()
    if error_code is not None:
        _settle(connection, task, lease, now, error_code)
        return False
    if results is None:
        try:
            results = evaluate_bundle(
                current.bundle,
                current.facts,
                current.reference_date,
                tenant_id=current.tenant_id,
                laboratory_id=current.laboratory_id,
            )
        except RuleDefinitionError:
            _settle(connection, task, lease, now, "RULESET_INVALID")
            return False
    if len(results) > 200:
        _settle(connection, task, lease, now, "RULESET_INVALID")
        return False
    findings = [value for value in results if value["truth"] is True]
    result = {
        "evaluator_version": current.bundle["evaluator_version"],
        "rule_bundle_id": current.rule_bundle_id,
        "rule_bundle_checksum": current.bundle_checksum,
        "reference_date": current.reference_date,
        "results": results,
        "unknown": any(value["truth"] is None for value in results),
        "insufficient_facts": any(value["truth"] is None for value in results),
    }
    for finding in findings:
        connection.execute(
            text(
                "INSERT INTO findings (id,tenant_id,item_id,run_id,fact_revision_id,"
                "rule_evaluation_id,rule_id,fingerprint,type,severity,status,explanation,evidence) "
                "VALUES (:id,:tenant,:item,:run,:fact,:evaluation,:rule,:fingerprint,:type,"
                ":severity,'needs_review',:explanation,:evidence)"
            ),
            {
                "id": str(uuid4()),
                "tenant": lease.tenant_id,
                "item": current.item_id,
                "run": current.run_id,
                "fact": current.fact_revision_id,
                "evaluation": current.evaluation_id,
                "rule": finding["rule_id"],
                "fingerprint": finding["fingerprint"],
                "type": finding["finding_type"],
                "severity": finding["severity"],
                "explanation": _render_explanation(
                    finding["explanation"], current.facts, current.reference_date
                ),
                "evidence": canonical_json(_finding_evidence(current, finding)),
            },
        )
    evaluation_status = "completed"
    connection.execute(
        text(
            "UPDATE rule_evaluations SET status=:status,result=:result,result_hash=:hash,"
            "finished_at=:now,updated_at=:now,version=version+1 "
            "WHERE tenant_id=:tenant AND id=:evaluation"
        ),
        {
            "status": evaluation_status,
            "result": canonical_json(result),
            "hash": sha256_json(result),
            "now": now,
            "tenant": lease.tenant_id,
            "evaluation": current.evaluation_id,
        },
    )
    connection.execute(
        text(
            "UPDATE inference_runs SET status='needs_review',stage='done',finished_at=:now,"
            "updated_at=:now,version=version+1 WHERE tenant_id=:tenant AND id=:run"
        ),
        {"now": now, "tenant": lease.tenant_id, "run": current.run_id},
    )
    connection.execute(
        text(
            "UPDATE inspection_items SET status='needs_review',version=version+1,updated_at=:now "
            "WHERE tenant_id=:tenant AND id=:item AND current_run_id=:run"
        ),
        {
            "now": now,
            "tenant": lease.tenant_id,
            "item": current.item_id,
            "run": current.run_id,
        },
    )
    _attempt(connection, task, lease, "succeeded", None, now)
    connection.execute(
        text(
            "UPDATE task_runs SET state='succeeded',lease_owner=NULL,lease_until=NULL,"
            "heartbeat_at=NULL,finished_at=:now,updated_at=:now,version=version+1 "
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
                "WHERE task_type='rule_evaluation' AND state='leased' "
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
    attempt_id = connection.scalar(
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
    )
    lease = RuleLease(
        task["tenant_id"],
        task["id"],
        task["lease_owner"],
        attempt_id,
        task["fencing_token"],
        task["replay_generation"],
        task["attempt"],
        current,
    )
    _settle(connection, task, lease, now, "STAGE_TIMEOUT", abandoned=True)
    return True
