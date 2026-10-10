"""Focused MySQL acceptance for the inference execution fence."""

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from packages.domain.inference_execution import InferenceLeaseLost
from packages.persistence import inference_execution, rule_execution
from tests.evidence_helpers import (
    add_bottles,
    add_chemical_context,
    add_regions,
    artifacts_for,
    synthetic_chemical_entries,
)
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
@pytest.mark.parametrize("outcome", ["facts_ready", "needs_review"])
@pytest.mark.parametrize("simulated", [False, True])
@pytest.mark.parametrize("association", ["none", "unique", "tie"])
def test_inference_claim_and_facts_commit_are_fenced(
    identity, database, outcome, simulated, association
):
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
            if outcome == "needs_review":
                synthetic = {
                    "model_bundle_id": model,
                    "dictionary_version_id": dictionary,
                    "ocr_fields": [],
                    "text_regions": [],
                    "pipeline_version": "vision-v1",
                }
                add_chemical_context(synthetic, synthetic_chemical_entries())
                connection.execute(
                    text("UPDATE dictionary_versions SET checksum=:sha WHERE id=:id"),
                    {"id": dictionary, "sha": synthetic["dictionary_sha256"]},
                )
                connection.execute(
                    text("UPDATE model_versions SET checksum=:sha WHERE id=:id"),
                    {"id": model, "sha": synthetic["model_checksum"]},
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
    # Every rejected mutation is rolled back, so the original frozen run remains executable.
    for table, value in (("run_images", None), ("asset_images", "analysis-v2")):
        with database.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    text(
                        f"UPDATE {table} SET analysis_object_version=:value WHERE "
                        + ("run_id=:id" if table == "run_images" else "id=:id")
                    ),
                    {"value": value, "id": run["id"] if table == "run_images" else image},
                )
                assert inference_execution.claim(connection, message, str(uuid4())) is None
                assert (
                    connection.scalar(
                        text("SELECT COUNT(*) FROM task_attempts WHERE task_id=:id"),
                        {"id": message["task_id"]},
                    )
                    == 0
                )
            finally:
                transaction.rollback()
    with database.begin() as connection:
        lease = inference_execution.claim(connection, message, str(uuid4()))
        assert lease is not None
        assert lease.input.images[0].object_version == "analysis-v1"
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
            "outcome": outcome,
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
            "text_regions": [],
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
        # Synthetic wire metadata tests persistence/projection, not real numerical execution.
        from tests.ai.test_supervisor import IDENTITY

        result["execution_identity"] = dict(
            IDENTITY,
            is_simulated=simulated,
            **{
                key: result[key]
                for key in (
                    "model_bundle_id",
                    "model_checksum",
                    "dictionary_version_id",
                    "dictionary_sha256",
                    "pipeline_version",
                )
            },
        )
        add_regions(lease, result)
        if association != "none":
            add_bottles(lease, result, 1 if association == "unique" else 2)
        if outcome == "needs_review":
            result["text_regions"][0].update(raw_text="NAME", confidence=0.9)
            result["text_regions"][1].update(raw_text="Alpha", confidence=0.3)
            add_chemical_context(result, synthetic_chemical_entries())
            result["execution_identity"]["is_simulated"] = simulated
            assert result["model_checksum"] == lease.input.model_checksum
            assert result["dictionary_sha256"] == lease.input.dictionary_sha256
        artifacts = artifacts_for(lease, result)
        for mutation in (
            "UPDATE task_runs SET lease_until=UTC_TIMESTAMP(3)-INTERVAL 1 SECOND WHERE id=:id",
            "UPDATE inference_runs SET status='superseded' WHERE id=:id",
        ):
            savepoint = connection.begin_nested()
            connection.execute(
                text(mutation),
                {"id": lease.task_id if "task_runs" in mutation else lease.input.run_id},
            )
            with pytest.raises(InferenceLeaseLost):
                inference_execution.commit_result(connection, lease, result, artifacts)
            savepoint.rollback()
        savepoint = connection.begin_nested()
        connection.execute(
            text("UPDATE asset_images SET analysis_object_version='analysis-v2' WHERE id=:id"),
            {"id": image},
        )
        with pytest.raises(InferenceLeaseLost):
            inference_execution.commit_result(connection, lease, result, artifacts)
        savepoint.rollback()

        # Every derivative and all success state must roll back if a later DB write fails.
        def fail_after_crops(conn, cursor, statement, parameters, context, many):
            if statement.startswith("UPDATE inference_runs SET status="):
                raise RuntimeError("Injected result write fault")

        savepoint = connection.begin_nested()
        event.listen(connection, "before_cursor_execute", fail_after_crops)
        try:
            with pytest.raises(RuntimeError, match="Injected"):
                inference_execution.commit_result(connection, lease, result, artifacts)
        finally:
            event.remove(connection, "before_cursor_execute", fail_after_crops)
            savepoint.rollback()
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM image_derivatives WHERE run_id=:run"),
                {"run": lease.input.run_id},
            )
            == 0
        )
        assert (
            connection.scalar(
                text("SELECT state FROM task_runs WHERE id=:task"), {"task": lease.task_id}
            )
            == "leased"
        )
        for field, wrong in (
            ("owner", str(uuid4())),
            ("token", lease.token + 1),
            ("attempt_id", str(uuid4())),
            ("generation", lease.generation + 1),
            ("attempt", lease.attempt + 1),
        ):
            from dataclasses import replace

            stale = replace(lease, **{field: wrong})
            stale_result = {**result, "attempt_id": stale.attempt_id, "fencing_token": stale.token}
            with pytest.raises(InferenceLeaseLost):
                inference_execution.commit_result(connection, stale, stale_result, artifacts)
        inference_execution.commit_result(connection, lease, result, artifacts)
        stored = json.loads(
            connection.scalar(
                text("SELECT result FROM inference_runs WHERE id=:run"),
                {"run": lease.input.run_id},
            )
        )
        assert stored["ocr_fields"] == result["ocr_fields"]
        assert stored["entities"] == result["entities"]
        if outcome == "needs_review" and association == "unique":
            assert len(stored["ocr_fields"][0]["source_lines"]) == 2
            assert stored["ocr_fields"][0]["confidence"] == 0.3
            assert stored["entities"][0]["resolution"] == "candidate"
            assert stored["extraction_context"] == result["extraction_context"]
        registered = (
            connection.execute(
                text(
                    "SELECT crop_id,line_id,detection_id,object_key,object_version,"
                    "sha256,size_bytes,recipe "
                    "FROM image_derivatives WHERE tenant_id=:tenant AND run_id=:run"
                ),
                {"tenant": lease.tenant_id, "run": lease.input.run_id},
            )
            .mappings()
            .all()
        )
        assert len(registered) == 2
        expected_detection = (
            result["detections"][0]["detection_id"] if association == "unique" else None
        )
        assert all(
            row["detection_id"] == expected_detection and row["line_id"] for row in registered
        )
        crops_by_id = {row["crop_id"]: row for row in result["crops"]}
        assert all(json.loads(row["recipe"]) == crops_by_id[row["crop_id"]] for row in registered)
        assert {row["object_version"] for row in registered} == {artifacts[0].object_version}
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
        assert state.run_status == ("processing" if outcome == "facts_ready" else "needs_review")
        assert state.stage == ("rules" if outcome == "facts_ready" else "done")
        assert state.state == "succeeded"
        assert state.item_status == ("processing" if outcome == "facts_ready" else "needs_review")
        assert (state.current_fact_revision_id is not None) == (outcome == "facts_ready")
        from packages.persistence.inference_runs import _projection

        assert (
            _projection(connection, lease.tenant_id, lease.input.run_id)["text_regions"]
            == result["text_regions"]
        )
        assert (
            _projection(connection, lease.tenant_id, lease.input.run_id)["is_simulated"]
            == simulated
        )
    if outcome == "needs_review":
        return
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
