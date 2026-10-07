"""Real MySQL acceptance for the remediation task lifecycle."""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.persistence.factories import insert
from tests.persistence.test_identity_mysql import ORIGIN, login
from tests.persistence.test_item_queries_mysql import inspection
from tests.persistence.test_roles_mysql import command, create_user, role_write


def post(http, session, path, body, *, key=None):
    return http.post(
        f"/api/v1{path}",
        json=body,
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": session["data"]["csrf_token"],
            "Idempotency-Key": key or str(uuid4()),
        },
    )


def finding_case(http, database, tenant, session):
    value, lab, _, _ = inspection(http, session, count=1)
    item_id = value["item_ids"][0]
    with database.begin() as connection:
        dictionary = insert(
            connection, "dictionary_versions", tenant_id=tenant["tenant_id"], status="published"
        )
        model = insert(
            connection,
            "model_versions",
            tenant_id=tenant["tenant_id"],
            dictionary_version_id=dictionary,
            status="published",
        )
        rules = insert(connection, "rule_bundles", tenant_id=tenant["tenant_id"])
        run = insert(
            connection,
            "inference_runs",
            tenant_id=tenant["tenant_id"],
            item_id=item_id,
            laboratory_id=lab["id"],
            model_bundle_id=model,
            dictionary_version_id=dictionary,
            rule_bundle_id=rules,
            status="needs_review",
            stage="done",
            result=json.dumps({"detections": []}),
        )
        fact = insert(
            connection,
            "fact_revisions",
            tenant_id=tenant["tenant_id"],
            item_id=item_id,
            run_id=run,
            entities="[]",
            relations="[]",
            dates="[]",
        )
        evaluation = insert(
            connection,
            "rule_evaluations",
            tenant_id=tenant["tenant_id"],
            item_id=item_id,
            run_id=run,
            fact_revision_id=fact,
            rule_bundle_id=rules,
            status="completed",
        )
        finding = insert(
            connection,
            "findings",
            tenant_id=tenant["tenant_id"],
            item_id=item_id,
            run_id=run,
            fact_revision_id=fact,
            rule_evaluation_id=evaluation,
            status="confirmed",
            evidence=json.dumps({"raw_facts": {"entities": [], "relations": []}}),
        )
    return finding, lab["id"]


@pytest.mark.usefixtures("database")
def test_dispatch_idempotency_and_atomic_write_set(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        admin = login(http, tenant).json()
        user = create_user(http, admin, "remediator")
        value, lab = finding_case(http, database, tenant, admin)
        role_result = role_write(http, admin, user["id"], command(role="remediator", lab=lab))
        assert role_result.status_code == 200
        due_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        body = {
            "expected_version": 1,
            "assignee_id": user["id"],
            "due_at": due_at,
            "priority": "high",
            "description": "Contain the confirmed risk",
        }
        key = "remediation-dispatch-1"
        first = post(http, admin, f"/findings/{value}/remediation-tasks", body, key=key)
        replay = post(http, admin, f"/findings/{value}/remediation-tasks", body, key=key)
        assert first.status_code == replay.status_code == 201, first.text
        assert first.content == replay.content
        task_id = first.json()["data"]["id"]
        with database.connect() as connection:
            state = (
                connection.execute(
                    text(
                        "SELECT t.status,t.version,f.status AS finding_status,"
                        "(SELECT COUNT(*) FROM outbox_events WHERE aggregate_id=:task) AS events,"
                        "(SELECT COUNT(*) FROM notifications "
                        "WHERE resource_id=:task) AS notifications,"
                        "(SELECT COUNT(*) FROM audit_events WHERE resource_id=:task) AS audits "
                        "FROM remediation_tasks t JOIN findings f ON f.id=t.finding_id "
                        "WHERE t.id=:task"
                    ),
                    {"task": task_id},
                )
                .mappings()
                .one()
            )
        assert dict(state) == {
            "status": "pending_dispatch",
            "version": 1,
            "finding_status": "dispatched",
            "events": 1,
            "notifications": 1,
            "audits": 1,
        }
        replacement = create_user(http, admin, "replacement")
        role_result = role_write(
            http, admin, replacement["id"], command(role="remediator", lab=lab)
        )
        assert role_result.status_code == 200
        reassigned = post(
            http,
            admin,
            f"/remediation-tasks/{task_id}/reassign",
            {
                "expected_version": 1,
                "assignee_id": replacement["id"],
                "due_at": due_at,
                "reason": "Move to the replacement remediator",
            },
        )
        assert reassigned.status_code == 200, reassigned.text
        assert reassigned.json()["data"]["assignee_id"] == replacement["id"]
        with database.connect() as connection:
            notification = connection.execute(
                text(
                    "SELECT recipient_id FROM notifications "
                    "WHERE resource_id=:task ORDER BY created_at DESC LIMIT 1"
                ),
                {"task": task_id},
            ).scalar_one()
        assert notification == replacement["id"]


@pytest.mark.usefixtures("database")
def test_remediation_accept_submit_and_recheck_are_fenced(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        admin = login(http, tenant).json()
        user = create_user(http, admin, "remediator")
        finding, lab = finding_case(http, database, tenant, admin)
        role_result = role_write(http, admin, user["id"], command(role="remediator", lab=lab))
        assert role_result.status_code == 200
        due_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        dispatched = post(
            http,
            admin,
            f"/findings/{finding}/remediation-tasks",
            {
                "expected_version": 1,
                "assignee_id": user["id"],
                "due_at": due_at,
                "priority": "medium",
                "description": "Investigate",
            },
        )
        assert dispatched.status_code == 201, dispatched.text
        task_id = dispatched.json()["data"]["id"]
        remediator = login(http, tenant, username="remediator").json()
        accepted = post(
            http,
            remediator,
            f"/remediation-tasks/{task_id}/accept",
            {
                "expected_version": 1,
                "reason": "Accepted for remediation",
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["data"]["status"] == "in_progress"

        with database.begin() as connection:
            upload = insert(
                connection,
                "uploads",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab,
                remediation_task_id=task_id,
                requested_by=user["id"],
                object_key=f"staging/{uuid4()}",
                expected_sha256="b" * 64,
                mime_type="image/png",
                size_bytes=100,
            )
            image = insert(
                connection,
                "asset_images",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab,
                remediation_task_id=task_id,
                upload_id=upload,
                status="ready",
                original_key=f"original/{uuid4()}",
                original_object_version="original-v1",
                original_sha256="b" * 64,
                analysis_key=f"analysis/{uuid4()}",
                analysis_object_version="analysis-v1",
                analysis_sha256="a" * 64,
                width=100,
                height=100,
                mime_type="image/png",
            )
        submitted = post(
            http,
            remediator,
            f"/remediation-tasks/{task_id}/submit-evidence",
            {
                "expected_version": 2,
                "image_ids": [image],
                "description": "Containment evidence",
            },
        )
        assert submitted.status_code == 200, submitted.text
        evidence = submitted.json()["data"]
        assert evidence["image_ids"] == [image]
        stale = post(
            http,
            remediator,
            f"/remediation-tasks/{task_id}/submit-evidence",
            {
                "expected_version": 2,
                "image_ids": [image],
                "description": "Stale retry",
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

        admin = login(http, tenant).json()
        checked = post(
            http,
            admin,
            f"/remediation-tasks/{task_id}/recheck",
            {
                "expected_version": 3,
                "evidence_id": evidence["id"],
                "reason": "Evidence verified",
            },
        )
        assert checked.status_code == 200, checked.text
        assert checked.json()["data"]["status"] == "closed"
        with database.connect() as connection:
            state = (
                connection.execute(
                    text(
                        "SELECT t.status,t.version,f.status AS finding_status "
                        "FROM remediation_tasks t JOIN findings f ON f.id=:finding WHERE t.id=:task"
                    ),
                    {"finding": finding, "task": task_id},
                )
                .mappings()
                .one()
            )
        assert dict(state) == {"status": "closed", "version": 4, "finding_status": "closed"}
