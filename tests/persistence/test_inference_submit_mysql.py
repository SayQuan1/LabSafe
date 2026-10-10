"""Real MySQL acceptance for the atomic submitInspectionItem command."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import text

from packages.shared.json_hash import canonical_json
from tests.persistence.factories import insert
from tests.persistence.test_identity_mysql import ORIGIN, login
from tests.persistence.test_item_queries_mysql import inspection


def post(http, session, path, body, *, key=None):
    headers = {
        "Origin": ORIGIN,
        "X-CSRF-Token": session["data"]["csrf_token"],
        "Idempotency-Key": key or str(uuid4()),
    }
    return http.post(f"/api/v1{path}", json=body, headers=headers)


@pytest.mark.usefixtures("database")
def test_submit_creates_pinned_run_task_and_outbox(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, lab, room, _ = inspection(http, session, count=1)
        item_id = value["item_ids"][0]
        with database.begin() as connection:
            upload_id = insert(
                connection,
                "uploads",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab["id"],
                inspection_item_id=item_id,
                requested_by=tenant["admin_id"],
                object_key=f"staging/{tenant['tenant_id']}/{uuid4()}",
                expected_sha256="b" * 64,
                mime_type="image/png",
                size_bytes=100,
            )
            image_id = insert(
                connection,
                "asset_images",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab["id"],
                inspection_item_id=item_id,
                upload_id=upload_id,
                status="ready",
                original_key=f"original/{tenant['tenant_id']}/{uuid4()}",
                original_object_version="original-v1",
                original_sha256="b" * 64,
                analysis_key=f"analysis/{tenant['tenant_id']}/{uuid4()}",
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
            insert(
                connection,
                "activations",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab["id"],
                model_bundle_id=model,
                dictionary_version_id=dictionary,
                rule_bundle_id=rules,
                pipeline_version="vision-v1",
                device_profile="cuda",
            )
        response = post(
            http,
            session,
            f"/inspection-items/{item_id}/submit",
            {
                "expected_version": 1,
                "images": [{"image_id": image_id, "role": "overview", "parent_image_id": None}],
            },
            key="submit-key-1",
        )
        assert response.status_code == 202, response.text
        run = response.json()["data"]
        assert run["status"] == "queued" and run["submission_revision"] == 1
        assert run["input_image_ids"] == [image_id]
        assert run["model_bundle_id"] == model and run["dictionary_version_id"] == dictionary
        with database.connect() as connection:
            frozen = (
                connection.execute(
                    text(
                        "SELECT analysis_sha256,analysis_object_version "
                        "FROM run_images WHERE run_id=:id"
                    ),
                    {"id": run["id"]},
                )
                .mappings()
                .one()
            )
            assert frozen == {"analysis_sha256": "a" * 64, "analysis_object_version": "analysis-v1"}
            inputs = (
                connection.execute(
                    text("SELECT * FROM inference_runs WHERE id=:id"), {"id": run["id"]}
                )
                .mappings()
                .one()
            )
            config = {
                key: inputs[key]
                for key in (
                    "model_bundle_id",
                    "dictionary_version_id",
                    "rule_bundle_id",
                    "pipeline_version",
                    "device_profile",
                )
            }
            config["reference_date"] = inputs["reference_date"].isoformat()
            config["images"] = [
                {"image_id": image_id, "role": "overview", "parent_image_id": None, **frozen}
            ]
            assert (
                inputs["input_hash"] == hashlib.sha256(canonical_json(config).encode()).hexdigest()
            )
            item = (
                connection.execute(
                    text("SELECT status,current_run_id FROM inspection_items WHERE id=:id"),
                    {"id": item_id},
                )
                .mappings()
                .one()
            )
            assert item["status"] == "queued" and item["current_run_id"] == run["id"]
            task = (
                connection.execute(
                    text("SELECT task_type,state FROM task_runs WHERE resource_id=:id"),
                    {"id": run["id"]},
                )
                .mappings()
                .one()
            )
            assert task == {"task_type": "inference_pipeline", "state": "ready"}
            event = connection.scalar(
                text("SELECT payload FROM outbox_events WHERE aggregate_id=:id"),
                {"id": run["job_id"]},
            )
            spec = json.loads(
                (Path(__file__).resolve().parents[2] / "contracts/task-message-v1.json").read_text()
            )
            Draft202012Validator(spec, format_checker=FormatChecker()).validate(json.loads(event))
