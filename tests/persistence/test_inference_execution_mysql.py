"""Focused MySQL acceptance for the inference execution fence."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from packages.persistence import inference_execution, rule_execution
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
def test_inference_claim_and_facts_commit_are_fenced(identity, database):
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
                object_key="staging/object",
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
                original_key="original/object",
                original_object_version="original-v1",
                original_sha256="b" * 64,
                analysis_key="tenant/x/lab/y/analysis/z/" + "a" * 64 + ".png",
                analysis_object_version="analysis-v1",
                analysis_sha256="a" * 64,
                width=100,
                height=100,
                mime_type="image/png",
            )
            connection.execute(
                text("UPDATE inspection_items SET status='uploaded' WHERE id=:id"),
                {"id": item_id},
            )
            dictionary = insert(
                connection,
                "dictionary_versions",
                tenant_id=tenant["tenant_id"],
                status="published",
            )
            model = insert(
                connection,
                "model_versions",
                tenant_id=tenant["tenant_id"],
                dictionary_version_id=dictionary,
                status="published",
            )
            rules = insert(connection, "rule_bundles", tenant_id=tenant["tenant_id"])
            rule_set = insert(connection, "rule_sets", tenant_id=tenant["tenant_id"])
            rule_version = insert(
                connection,
                "rule_versions",
                tenant_id=tenant["tenant_id"],
                rule_set_id=rule_set,
                status="published",
                definition=json.dumps(
                    {
                        "rule_id": "TEST-DATE",
                        "scope": "global",
                        "scope_id": None,
                        "priority": 1,
                        "enabled": True,
                        "effective_from": "2026-01-01T00:00:00Z",
                        "effective_to": None,
                        "clauses": [[{"field": "expiry_date", "op": "before_reference_date"}]],
                        "finding_type": "expired_label",
                        "severity": "low",
                        "action_code": "review_date",
                        "explanation_template": "test",
                        "source": "test",
                        "case_ids": ["T-1", "T-2", "T-3"],
                    }
                ),
            )
            insert(
                connection,
                "rule_bundle_members",
                tenant_id=tenant["tenant_id"],
                bundle_id=rules,
                rule_version_id=rule_version,
            )
            insert(
                connection,
                "activations",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab["id"],
                model_bundle_id=model,
                dictionary_version_id=dictionary,
                rule_bundle_id=rules,
                pipeline_version="vision-v1",
                device_profile="cpu",
            )
        response = post(
            http,
            session,
            f"/inspection-items/{item_id}/submit",
            {
                "expected_version": 1,
                "images": [{"image_id": image, "role": "overview", "parent_image_id": None}],
            },
        )
        assert response.status_code == 202, response.text
        run = response.json()["data"]
    with database.connect() as connection:
        message = json.loads(
            connection.scalar(
                text("SELECT payload FROM outbox_events WHERE aggregate_id=:task"),
                {"task": run["job_id"]},
            )
        )
    with database.begin() as connection:
        lease = inference_execution.claim(connection, message, str(uuid4()))
        assert lease is not None
        result = {
            "run_id": lease.input.run_id,
            "attempt_id": lease.attempt_id,
            "fencing_token": lease.token,
            "tenant_id": lease.input.tenant_id,
            "request_hash": lease.input.request_hash,
            "input_hashes": [
                {"image_id": value.image_id, "sha256": value.sha256} for value in lease.input.images
            ],
            "model_bundle_id": lease.input.model_bundle_id,
            "model_checksum": lease.input.model_checksum,
            "dictionary_version_id": lease.input.dictionary_version_id,
            "dictionary_sha256": lease.input.dictionary_sha256,
            "pipeline_version": "vision-v1",
            "outcome": "facts_ready",
            "quality": [
                {
                    "image_id": image,
                    "status": "pass",
                    "reasons": [],
                    "blur_score": 100,
                    "brightness": 0.5,
                    "glare_ratio": 0,
                }
            ],
            "detections": [],
            "crops": [],
            "ocr_fields": [],
            "entities": [],
            "relations": [],
            "timing_ms": {
                key: 0
                for key in (
                    "download_ms",
                    "quality_ms",
                    "detection_ms",
                    "ocr_ms",
                    "normalization_ms",
                    "total_ms",
                )
            },
        }
        inference_execution.commit_result(connection, lease, result)
        state = connection.execute(
            text(
                "SELECT r.status AS run_status,r.stage,t.state,i.status AS item_status,"
                "i.current_fact_revision_id "
                "FROM inference_runs r JOIN task_runs t ON t.resource_id=r.id "
                "AND t.task_type='inference_pipeline' "
                "JOIN inspection_items i ON i.id=r.item_id WHERE r.id=:run"
            ),
            {"run": lease.input.run_id},
        ).one()
        assert state.run_status == "processing"
        assert state.stage == "rules"
        assert state.state == "succeeded"
        assert state.item_status == "processing"
        assert state.current_fact_revision_id is not None
    with database.connect() as connection:
        rule_message = json.loads(
            connection.scalar(
                text(
                    "SELECT payload FROM outbox_events "
                    "WHERE JSON_UNQUOTE(JSON_EXTRACT(payload,'$.task_type'))='rule_evaluation' "
                    "AND tenant_id=:tenant ORDER BY created_at DESC LIMIT 1"
                ),
                {"tenant": tenant["tenant_id"]},
            )
        )
    with database.begin() as connection:
        rule_lease = rule_execution.claim(connection, rule_message, str(uuid4()))
        assert rule_lease is not None
        assert rule_execution.commit_result(connection, rule_lease)
        status = connection.execute(
            text(
                "SELECT e.status AS evaluation_status,r.status AS run_status,"
                "i.status AS item_status "
                "FROM rule_evaluations e JOIN inference_runs r ON r.id=e.run_id "
                "JOIN inspection_items i ON i.id=e.item_id WHERE e.id=:evaluation"
            ),
            {"evaluation": rule_lease.input.evaluation_id},
        ).one()
        assert status.evaluation_status == "completed"
        assert status.run_status == "needs_review"
        assert status.item_status == "needs_review"
