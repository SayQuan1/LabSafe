"""Execution values for validate_image; no queue or database dependencies."""

import random
from dataclasses import dataclass
from datetime import timedelta


class LeaseLost(RuntimeError):
    def __init__(self):
        super().__init__("LEASE_LOST")


class InvalidDispatch(ValueError):
    def __init__(self):
        super().__init__("Invalid or future task dispatch")


# This subset handles infrastructure failures, not image-validation outcomes.
# Content errors must atomically produce rejected image/upload via the handler.
TECHNICAL_ERRORS = {"DEPENDENCY_UNAVAILABLE": True, "STAGE_TIMEOUT": True, "INTERNAL_ERROR": False}


def retry_delay(attempt, *, uniform=random.uniform):
    if type(attempt) is not int or attempt not in (1, 2, 3):
        raise ValueError("Retry delay requires attempt 1..3")
    seconds = (5, 30, 120)[attempt - 1]
    jitter = uniform(0, 0.2)
    if not 0 <= jitter <= 0.2:
        raise ValueError("Invalid jitter")
    return timedelta(milliseconds=int(seconds * (1 + jitter) * 1000))


@dataclass(frozen=True)
class ImageInput:
    image_id: str
    upload_id: str
    laboratory_id: str
    owner_type: str
    owner_id: str
    key: str
    object_version: str
    expected_sha256: str
    size_bytes: int
    mime_type: str
    image_version: int
    upload_version: int


@dataclass(frozen=True)
class ImageLease:
    tenant_id: str
    task_id: str
    owner: str
    token: int
    generation: int
    attempt: int
    input: ImageInput
