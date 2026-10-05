"""Focused MySQL acceptance for facts and finding review transactions."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.persistence.factories import insert
from tests.persistence.test_identity_mysql import ORIGIN, login
from tests.persistence.test_item_queries_mysql import inspection


def post(http, session, path, body):
    return http.post(
        f"/api/v1{path}",
        json=body,
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": session["data"]["csrf_token"],
            "Idempotency-Key": str(uuid4()),
        },
    )


@pytest.mark.usefixtures("database")
def test_finding_decision_and_fact_revision_are_atomic(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, lab, _, _ = inspection(http, session, count=1)
        item_id = value["item_ids"][0]
        with database.begin() as connection:
            upload = insert(
                connection,
                "uploads",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab["id"],
                inspection_item_id=item_id,
                requested_by=tenant["admin_id"],
                object_key="staging/review",
                expected_sha256="b" * 64,
                mime_type="image/png",
                size_bytes=100,
            )
            image = insert(
                connection,
                "asset_images",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab["id"],
                inspection_item_id=item_id,
                upload_id=upload,
                status="ready",
                original_key="original/review",
                original_object_version="original-v1",
                original_sha256="b" * 64,
                analysis_key="analysis/review",
                analysis_object_version="analysis-v1",
                analysis_sha256="a" * 64,
                width=100,
                height=100,
                mime_type="image/png",
            )
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
            bundle = insert(connection, "rule_bundles", tenant_id=tenant["tenant_id"])
            run = insert(
                connection,
                "inference_runs",
                tenant_id=tenant["tenant_id"],
                item_id=item_id,
                laboratory_id=lab["id"],
                model_bundle_id=model,
                dictionary_version_id=dictionary,
                rule_bundle_id=bundle,
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
                rule_bundle_id=bundle,
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
                evidence=json.dumps({"raw_facts": {"entities": [], "relations": []}}),
            )
            insert(
                connection,
                "run_images",
                tenant_id=tenant["tenant_id"],
                item_id=item_id,
                run_id=run,
                image_id=image,
            )
            connection.execute(
                text(
                    "UPDATE inspection_items SET status='needs_review',current_run_id=:run,"
                    "current_fact_revision_id=:fact,current_evaluation_id=:evaluation "
                    "WHERE id=:item"
                ),
                {"run": run, "fact": fact, "evaluation": evaluation, "item": item_id},
            )

        response = http.get(f"/api/v1/findings/{finding}")
        assert response.status_code == 200, response.text
        assert response.json()["data"]["evidence"][0]["image_id"] == image

        response = post(
            http,
            session,
            f"/findings/{finding}/reject",
            {"expected_version": 1, "reason": "Reviewed"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "rejected"

        with database.connect() as connection:
            item_version = connection.scalar(
                text("SELECT version FROM inspection_items WHERE id=:id"), {"id": item_id}
            )
        response = post(
            http,
            session,
            f"/inspection-items/{item_id}/facts",
            {
                "expected_version": item_version,
                "entities": [],
                "relations": [],
                "dates": [],
                "reason": "No facts require correction",
            },
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["data"]["id"]
        with database.connect() as connection:
            state = (
                connection.execute(
                    text(
                        "SELECT i.status,i.current_fact_revision_id,i.current_evaluation_id,"
                        "f.superseded_at,t.task_type,t.state "
                        "FROM inspection_items i JOIN findings f ON f.id=:finding "
                        "JOIN task_runs t ON t.id=:task WHERE i.id=:item"
                    ),
                    {"finding": finding, "task": task_id, "item": item_id},
                )
                .mappings()
                .one()
            )
        assert state["status"] == "processing"
        assert state["superseded_at"] is not None
        assert state["task_type"] == "rule_evaluation" and state["state"] == "ready"
