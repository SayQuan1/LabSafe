from datetime import datetime, timezone
from uuid import uuid4

import pytest

from packages.domain.inference_execution import (
    InferenceInvalidResult,
    request_payload,
    validate_result,
)
from tests.evidence_helpers import lease, result_for


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
