"""Synthetic contract fixture. No image download, inference or business writes."""

import asyncio
from datetime import datetime, timezone
from typing import Any

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.inputs import verify_image_inputs
from packages.inference_protocol.contract import DOCUMENT

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
        raise ProtocolError("FORBIDDEN", "Tenant is not allowed on this instance")
    for key in ("model_bundle_id", "dictionary_version_id", "pipeline_version", "device_profile"):
        if payload[key] != settings.identity[key]:
            raise ProtocolError(
                "MODEL_VERSION_UNAVAILABLE", "Request does not match loaded identity"
            )
    if (
        payload["model_checksum"] != settings.model_checksum
        or payload["dictionary_sha256"] != settings.dictionary_sha256
    ):
        raise ProtocolError("HASH_MISMATCH", "Loaded artifact hash mismatch")
    try:
        verify_image_inputs(payload, settings.allowed_tenants)
    except AdapterError as error:
        raise ProtocolError(error.code, str(error)) from None


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
