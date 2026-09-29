"""Synthetic contract fixture. No image download, inference or business writes."""

import asyncio
from datetime import datetime, timezone
from typing import Any

from packages.inference_protocol.contract import DOCUMENT
from packages.inference_protocol.hashing import request_hash

from .settings import Settings


class ProtocolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        self.status = DOCUMENT["x-error-codes"][code]["http_status"]
        self.retryable = DOCUMENT["x-error-codes"][code]["retryable"]
        super().__init__(message)


def verify_request(payload: dict[str, Any], settings: Settings) -> None:
    if payload["tenant_id"] not in settings.allowed_tenants:
        raise ProtocolError("FORBIDDEN", "Tenant is not allowed on this fixture instance")
    for key in ("model_bundle_id", "dictionary_version_id", "pipeline_version", "device_profile"):
        if payload[key] != settings.identity[key]:
            raise ProtocolError(
                "MODEL_VERSION_UNAVAILABLE", "Request does not match loaded fixture"
            )
    if (
        payload["model_checksum"] != settings.model_checksum
        or payload["dictionary_sha256"] != settings.dictionary_sha256
    ):
        raise ProtocolError("HASH_MISMATCH", "Fixture artifact hash mismatch")
    if payload["request_hash"] != request_hash(payload):
        raise ProtocolError("HASH_MISMATCH", "Request content hash mismatch")
    refs = payload["image_refs"]
    overview = refs[0]
    if overview["role"] != "overview" or overview["parent_image_id"] is not None:
        raise ProtocolError("VALIDATION_ERROR", "First image must be the unique overview")
    if len({image["image_id"] for image in refs}) != len(refs):
        raise ProtocolError("VALIDATION_ERROR", "Image IDs must be unique")
    for index, image in enumerate(refs):
        expected_key = (
            f"tenant/{payload['tenant_id']}/lab/{payload['laboratory_id']}/analysis/"
            f"{image['image_id']}/{image['sha256']}.png"
        )
        if image["object_key"] != expected_key:
            raise ProtocolError("UNAUTHORIZED_REF", "Only exact analysis object keys are accepted")
        if image["mime_type"] != "image/png":
            raise ProtocolError("VALIDATION_ERROR", "Analysis images must be normalized PNG")
        if image["location_id"] != overview["location_id"]:
            raise ProtocolError("VALIDATION_ERROR", "Images must share a location")
        if index and (
            image["role"] != "detail" or image["parent_image_id"] != overview["image_id"]
        ):
            raise ProtocolError("VALIDATION_ERROR", "Detail must refer to the overview")


async def compute(payload: dict[str, Any], settings: Settings, stage: str) -> dict[str, Any]:
    remaining = (
        datetime.fromisoformat(payload["deadline_at"].upper().replace("Z", "+00:00"))
        - datetime.now(timezone.utc)
    ).total_seconds()
    if remaining <= 0:
        raise ProtocolError("AI_TIMEOUT", "Request deadline has expired")
    budget = min(remaining, 10 if stage == "quality" else 180)
    delay = budget + 1 if settings.scenario == "timeout" else settings.delay_ms / 1000
    try:
        # Cooperative synthetic delay only. Real compute cancellation belongs to I-03.
        await asyncio.wait_for(asyncio.sleep(delay), timeout=budget)
    except TimeoutError as exc:
        raise ProtocolError("AI_TIMEOUT", "Fixture deadline exceeded") from exc
    if settings.scenario == "error":
        raise ProtocolError("MODEL_ERROR", "Configured synthetic fixture failure")
    retake = settings.scenario == "needs_retake"
    echoed = (
        "run_id",
        "attempt_id",
        "fencing_token",
        "tenant_id",
        "request_hash",
        "model_bundle_id",
        "model_checksum",
        "dictionary_version_id",
        "dictionary_sha256",
        "pipeline_version",
    )
    return {
        **{key: payload[key] for key in echoed},
        "input_hashes": [
            {"image_id": image["image_id"], "sha256": image["sha256"]}
            for image in payload["image_refs"]
        ],
        "outcome": "needs_retake"
        if retake
        else ("facts_ready" if stage == "quality" else "needs_review"),
        "quality": [
            {
                "image_id": image["image_id"],
                "status": "needs_retake" if retake else "pass",
                "reasons": ["blur"] if retake else [],
                "blur_score": 0 if retake else 100,
                "brightness": 0.5,
                "glare_ratio": 0,
            }
            for image in payload["image_refs"]
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
