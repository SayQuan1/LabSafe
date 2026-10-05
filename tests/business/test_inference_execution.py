from datetime import datetime, timezone
from uuid import uuid4

import pytest

from packages.domain.inference_execution import (
    InferenceImage,
    InferenceInput,
    InferenceInvalidResult,
    InferenceLease,
    request_payload,
    validate_result,
)


def lease():
    tenant, lab, item, run, image = (str(uuid4()) for _ in range(5))
    value = InferenceInput(
        tenant_id=tenant,
        laboratory_id=lab,
        item_id=item,
        run_id=run,
        submission_revision=2,
        model_bundle_id=str(uuid4()),
        model_checksum="a" * 64,
        dictionary_version_id=str(uuid4()),
        dictionary_sha256="b" * 64,
        pipeline_version="vision-v1",
        device_profile="cpu",
        images=(
            InferenceImage(
                image_id=image,
                object_key=f"tenant/{tenant}/lab/{lab}/analysis/{image}/{'c' * 64}.png",
                sha256="c" * 64,
                mime_type="image/png",
                role="overview",
                location_id=str(uuid4()),
                parent_image_id=None,
            ),
        ),
    )
    return InferenceLease(tenant, str(uuid4()), str(uuid4()), str(uuid4()), 3, 0, 1, value)


def result_for(value, outcome="needs_retake"):
    image = value.input.images[0]
    return {
        "run_id": value.input.run_id,
        "attempt_id": value.attempt_id,
        "fencing_token": value.token,
        "tenant_id": value.input.tenant_id,
        "request_hash": value.input.request_hash,
        "input_hashes": [{"image_id": image.image_id, "sha256": image.sha256}],
        "model_bundle_id": value.input.model_bundle_id,
        "model_checksum": value.input.model_checksum,
        "dictionary_version_id": value.input.dictionary_version_id,
        "dictionary_sha256": value.input.dictionary_sha256,
        "pipeline_version": "vision-v1",
        "outcome": outcome,
        "quality": [
            {
                "image_id": image.image_id,
                "status": "needs_retake" if outcome == "needs_retake" else "pass",
                "reasons": ["blur"] if outcome == "needs_retake" else [],
                "blur_score": 0 if outcome == "needs_retake" else 100,
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
            "download_ms": 0,
            "quality_ms": 0,
            "detection_ms": 0,
            "ocr_ms": 0,
            "normalization_ms": 0,
            "total_ms": 0,
        },
    }


def test_request_payload_contains_fixed_input_and_fence():
    value = lease()
    payload = request_payload(value, datetime(2026, 10, 4, tzinfo=timezone.utc))
    assert payload["attempt_id"] == value.attempt_id
    assert payload["fencing_token"] == value.token
    assert payload["request_hash"] == value.input.request_hash
    assert payload["image_refs"][0]["object_key"] == value.input.images[0].object_key


def test_result_accepts_exact_echo_and_input_hashes():
    value = lease()
    validate_result(result_for(value), value)


@pytest.mark.parametrize("field", ["run_id", "attempt_id", "fencing_token", "request_hash"])
def test_result_rejects_stale_fence_or_identity(field):
    value = lease()
    result = result_for(value)
    result[field] = str(uuid4()) if field != "fencing_token" else value.token + 1
    with pytest.raises(InferenceInvalidResult):
        validate_result(result, value)


def test_result_rejects_different_image_hash():
    value = lease()
    result = result_for(value)
    result["input_hashes"][0]["sha256"] = "d" * 64
    with pytest.raises(InferenceInvalidResult):
        validate_result(result, value)
