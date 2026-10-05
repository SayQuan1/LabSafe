"""Pure values and validation rules for inference pipeline execution."""

from dataclasses import dataclass
from datetime import datetime

from packages.inference_protocol.contract import validate
from packages.inference_protocol.hashing import request_hash


class InferenceLeaseLost(RuntimeError):
    def __init__(self):
        super().__init__("LEASE_LOST")


class InferenceInvalidResult(ValueError):
    def __init__(self):
        super().__init__("SCHEMA_MISMATCH")


# Codes which may safely return the task to retry_wait.  Version, hash and
# schema errors are deliberately absent: retrying the same invalid input
# cannot make it valid.
RETRYABLE_ERRORS = frozenset(
    {
        "MODEL_NOT_READY",
        "AI_BUSY",
        "AI_TIMEOUT",
        "MODEL_OOM",
        "DEPENDENCY_UNAVAILABLE",
        "STAGE_TIMEOUT",
    }
)
# Compatibility shape with the image execution domain: callers can classify a
# protocol error without knowing which codes are retryable.
TECHNICAL_ERRORS = {
    code: code in RETRYABLE_ERRORS
    for code in (
        *RETRYABLE_ERRORS,
        "MODEL_ERROR",
        "SCHEMA_MISMATCH",
        "HASH_MISMATCH",
        "MODEL_VERSION_UNAVAILABLE",
        "INTERNAL_ERROR",
    )
}


@dataclass(frozen=True)
class InferenceImage:
    image_id: str
    object_key: str
    sha256: str
    mime_type: str
    role: str
    location_id: str
    parent_image_id: str | None


@dataclass(frozen=True)
class InferenceInput:
    tenant_id: str
    laboratory_id: str
    item_id: str
    run_id: str
    submission_revision: int
    model_bundle_id: str
    model_checksum: str
    dictionary_version_id: str
    dictionary_sha256: str
    pipeline_version: str
    device_profile: str
    images: tuple[InferenceImage, ...]

    @property
    def request_hash(self) -> str:
        return request_hash(
            {
                "tenant_id": self.tenant_id,
                "laboratory_id": self.laboratory_id,
                "item_id": self.item_id,
                "run_id": self.run_id,
                "submission_revision": self.submission_revision,
                "model_bundle_id": self.model_bundle_id,
                "model_checksum": self.model_checksum,
                "dictionary_version_id": self.dictionary_version_id,
                "dictionary_sha256": self.dictionary_sha256,
                "pipeline_version": self.pipeline_version,
                "device_profile": self.device_profile,
                "image_refs": [image.__dict__ for image in self.images],
            }
        )


@dataclass(frozen=True)
class InferenceLease:
    tenant_id: str
    task_id: str
    owner: str
    attempt_id: str
    token: int
    generation: int
    attempt: int
    input: InferenceInput


def request_payload(lease: InferenceLease, deadline_at: datetime) -> dict:
    return {
        "run_id": lease.input.run_id,
        "attempt_id": lease.attempt_id,
        "fencing_token": lease.token,
        "tenant_id": lease.input.tenant_id,
        "laboratory_id": lease.input.laboratory_id,
        "item_id": lease.input.item_id,
        "submission_revision": lease.input.submission_revision,
        "model_bundle_id": lease.input.model_bundle_id,
        "model_checksum": lease.input.model_checksum,
        "dictionary_version_id": lease.input.dictionary_version_id,
        "dictionary_sha256": lease.input.dictionary_sha256,
        "pipeline_version": lease.input.pipeline_version,
        "device_profile": lease.input.device_profile,
        "deadline_at": deadline_at.isoformat().replace("+00:00", "Z"),
        "request_hash": lease.input.request_hash,
        "image_refs": [image.__dict__ for image in lease.input.images],
    }


def validate_result(result: dict, lease: InferenceLease) -> None:
    try:
        validate("InferenceResult", result)
    except ValueError as exc:
        raise InferenceInvalidResult() from exc
    expected = {
        "run_id": lease.input.run_id,
        "attempt_id": lease.attempt_id,
        "fencing_token": lease.token,
        "tenant_id": lease.input.tenant_id,
        "request_hash": lease.input.request_hash,
        "model_bundle_id": lease.input.model_bundle_id,
        "model_checksum": lease.input.model_checksum,
        "dictionary_version_id": lease.input.dictionary_version_id,
        "dictionary_sha256": lease.input.dictionary_sha256,
        "pipeline_version": lease.input.pipeline_version,
    }
    if any(result.get(key) != value for key, value in expected.items()):
        raise InferenceInvalidResult()
    hashes = result.get("input_hashes")
    expected_hashes = [
        {"image_id": image.image_id, "sha256": image.sha256} for image in lease.input.images
    ]
    if hashes != expected_hashes:
        raise InferenceInvalidResult()
