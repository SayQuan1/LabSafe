"""Wire closure, Worker failures, scope and artifact completeness without numeric deps."""

import copy
import json
from dataclasses import replace
from threading import Event
from unittest.mock import Mock
from uuid import UUID, uuid4, uuid5

import pytest

from apps.worker.inference_evidence import BoundedEvidencePrepare
from packages.application.inference_execution import InferenceExecution, InferenceHeartbeat
from packages.domain.inference_evidence import validate_derivatives
from packages.domain.inference_execution import (
    InferenceInvalidResult,
    InferenceLeaseLost,
    validate_result,
)
from packages.domain.security import ServiceError
from tests.business.test_inference_execution import lease, result_for
from tests.evidence_helpers import add_regions, artifacts_for


def test_independent_regions_have_no_detection_or_fact_and_replay_stably():
    value = lease()
    result = add_regions(value, result_for(value, "needs_review"))
    validate_result(result, value)
    assert not result["detections"] and not result["ocr_fields"] and not result["entities"]
    assert result == add_regions(value, result_for(value, "needs_review"))
    validate_derivatives(value, result, artifacts_for(value, result))


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_line",
        "duplicate_crop",
        "dangling_crop",
        "dangling_line",
        "dangling_detection",
        "foreign_image",
        "foreign_run",
        "foreign_tenant",
        "quad",
        "rotation",
        "dimension",
        "quality_missing",
        "duplicate_quality",
        "retake_with_regions",
        "field_crop",
        "entity",
        "relation",
        "101",
        "legacy_missing_regions",
        "legacy_missing_evidence",
    ],
)
def test_bad_reference_closure_and_capacity_rejected(case):
    value = lease()
    result = add_regions(value, result_for(value, "needs_review"))
    lines, crops = result["text_regions"], result["crops"]
    if case == "duplicate_line":
        lines[1] = copy.deepcopy(lines[0])
    elif case == "duplicate_crop":
        crops[1] = copy.deepcopy(crops[0])
    elif case == "dangling_crop":
        lines[0]["crop_id"] = str(uuid4())
    elif case == "dangling_line":
        crops[0]["line_id"] = str(uuid4())
    elif case == "dangling_detection":
        lines[0]["detection_id"] = crops[0]["detection_id"] = str(uuid4())
    elif case == "foreign_image":
        lines[0]["image_id"] = crops[0]["image_id"] = str(uuid4())
    elif case == "foreign_run":
        other = replace(value, input=replace(value.input, run_id=str(uuid4())))
        imported = add_regions(other, result_for(other, "needs_review"))
        lines[:], crops[:] = imported["text_regions"], imported["crops"]
    elif case == "foreign_tenant":
        result["tenant_id"] = str(uuid4())
    elif case == "quad":
        crops[0]["quad"] = copy.deepcopy(crops[0]["quad"])
        crops[0]["quad"][0]["x"] = 0.1
    elif case == "rotation":
        crops[0]["evidence"]["recognition_rotation_ccw"] = 90
    elif case == "dimension":
        crops[0]["evidence"]["recognition_width"] += 1
    elif case == "quality_missing":
        result["quality"] = []
    elif case == "duplicate_quality":
        result["quality"].append(result["quality"][0])
    elif case == "retake_with_regions":
        result["outcome"] = "needs_retake"
    elif case == "field_crop":
        result["ocr_fields"] = [
            {
                "image_id": value.input.images[0].image_id,
                "detection_id": str(uuid4()),
                "crop_id": str(uuid4()),
                "field": "name",
                "raw_text": "ethanol",
                "normalized_text": None,
                "confidence": 0.9,
            }
        ]
    elif case == "entity":
        result["entities"] = [
            {
                "image_id": value.input.images[0].image_id,
                "detection_id": str(uuid4()),
                "candidates": [],
                "resolution": "unknown",
            }
        ]
    elif case == "relation":
        result["relations"] = [
            {
                "image_id": value.input.images[0].image_id,
                "source_detection_id": str(uuid4()),
                "target_detection_id": str(uuid4()),
                "relation": "unknown",
                "confidence": 0,
            }
        ]
    elif case == "101":
        result = add_regions(value, result_for(value, "needs_review"), 101)
    elif case == "legacy_missing_regions":
        del result["text_regions"]
    elif case == "legacy_missing_evidence":
        del crops[0]["evidence"]
    with pytest.raises(InferenceInvalidResult):
        validate_result(result, value)


def test_exact_capacity_100_accepted():
    value = lease()
    validate_result(add_regions(value, result_for(value, "needs_review"), 100), value)


@pytest.mark.parametrize(
    "mode", ["ok", "cross_image", "foreign_detection_run", "duplicate", "cycle", "nan"]
)
def test_optional_real_detection_reference_is_same_image_and_current_run(mode):
    value = lease()
    first = value.input.images[0]
    other = replace(first, image_id=str(uuid4()), role="detail", parent_image_id=first.image_id)
    value = replace(value, input=replace(value.input, images=(first, other)))
    result = add_regions(value, result_for(value, "needs_review"))
    result["input_hashes"].append({"image_id": other.image_id, "sha256": other.sha256})
    result["quality"].append({**result["quality"][0], "image_id": other.image_id})
    image = other.image_id if mode == "cross_image" else first.image_id
    namespace = UUID(str(uuid4())) if mode == "foreign_detection_run" else UUID(value.input.run_id)
    detection = str(uuid5(namespace, f"{image}:bottle:0"))
    result["detections"] = [
        {
            "image_id": image,
            "detection_id": detection,
            "parent_detection_id": None,
            "class_id": 39,
            "type": "bottle",
            "bbox": [0, 0, 1, 1],
            "confidence": 0.9,
        }
    ]
    for row in result["text_regions"] + result["crops"]:
        row["detection_id"] = detection
    if mode == "duplicate":
        result["detections"].append(result["detections"][0])
    if mode == "cycle":
        result["detections"][0]["parent_detection_id"] = detection
    if mode == "nan":
        result["text_regions"][0]["confidence"] = float("nan")
    if mode == "ok":
        validate_result(result, value)
    else:
        with pytest.raises(InferenceInvalidResult):
            validate_result(result, value)


@pytest.mark.parametrize(
    "field",
    [
        "image_id",
        "crop_id",
        "line_id",
        "detection_id",
        "object_key",
        "object_version",
        "sha256",
        "size_bytes",
    ],
)
def test_artifact_scope_and_metadata_rejected(field):
    value = lease()
    result = add_regions(value, result_for(value, "needs_review"))
    artifacts = artifacts_for(value, result)
    bad = "null" if field == "object_version" else (99 if field == "size_bytes" else str(uuid4()))
    with pytest.raises(InferenceInvalidResult):
        validate_derivatives(value, result, (replace(artifacts[0], **{field: bad}), artifacts[1]))


def test_partial_and_duplicate_artifact_set_rejected():
    value = lease()
    result = add_regions(value, result_for(value, "needs_review"))
    artifacts = artifacts_for(value, result)
    for bad in ((), artifacts[:1], artifacts + artifacts[:1], (artifacts[0], artifacts[0])):
        with pytest.raises(InferenceInvalidResult):
            validate_derivatives(value, result, bad)


@pytest.mark.parametrize("mode", ["ok", "failure", "lost", "no_runtime"])
def test_orchestration_prepares_inside_heartbeat_and_never_commits_partial(monkeypatch, mode):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(InferenceHeartbeat, "interval", 0.01)
    value = lease()
    result = add_regions(value, result_for(value, "needs_review"))
    client = Mock()
    client.quality.return_value = result_for(value, "facts_ready")
    client.runs.return_value = result
    artifacts = artifacts_for(value, result)
    execution = InferenceExecution(Mock(), client)
    execution.claim = Mock(return_value=value)
    execution.commit = Mock()
    execution.fail = Mock()
    execution.heartbeat = Mock(return_value=mode != "lost")

    def prepare(lease, result, cancelled):
        assert lease == value
        if mode == "failure":
            raise ServiceError("HASH_MISMATCH", 422, "Synthetic second crop failure")
        if mode == "lost":
            assert cancelled.wait(2)
            return artifacts
        return artifacts

    execution.prepare = None if mode == "no_runtime" else prepare
    if mode == "ok":
        assert execution.execute({})
        execution.commit.assert_called_once_with(value, result, artifacts)
    else:
        with pytest.raises((ServiceError, InferenceLeaseLost, RuntimeError)):
            execution.execute({})
        execution.commit.assert_not_called()
        if mode == "lost":
            execution.fail.assert_not_called()
        else:
            execution.fail.assert_called_once()


@pytest.mark.parametrize("mode", ["timeout", "cancel", "success", "exit", "invalid"])
def test_bounded_process_is_killed_joined_and_result_validated(tmp_path, monkeypatch, mode):
    value = lease()
    result = add_regions(value, result_for(value, "needs_review"))
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"synthetic executable")
    cancel, child = Event(), Mock()
    child.poll.return_value = None if mode in {"timeout", "cancel"} else 0
    child.returncode = 1 if mode == "exit" else 0
    monkeypatch.setenv("DATABASE_URL_FILE", "must-not-inherit")
    monkeypatch.setenv("AI_S3_SECRET_KEY_FILE", "must-not-inherit")

    def start(args, **kwargs):
        assert "DATABASE_URL_FILE" not in kwargs["env"]
        assert "AI_S3_SECRET_KEY_FILE" not in kwargs["env"]
        assert json.loads(open(args[-2], encoding="utf-8").read())["result"] == result
        if mode == "cancel":
            cancel.set()
        artifacts = artifacts_for(value, result)
        output = {"artifacts": [row.__dict__ for row in artifacts]}
        if mode == "invalid":
            output["artifacts"] = output["artifacts"][:1]
        from pathlib import Path

        Path(args[-1]).write_text(json.dumps(output), encoding="utf-8")
        return child

    monkeypatch.setattr("apps.worker.inference_evidence.subprocess.Popen", start)
    monkeypatch.setattr(
        "apps.worker.inference_evidence.EVIDENCE_SECONDS", 0.01 if mode == "timeout" else 20
    )
    prepare = BoundedEvidencePrepare(str(executable))
    if mode == "success":
        assert prepare(value, result, cancel) == artifacts_for(value, result)
    else:
        with pytest.raises((ServiceError, InferenceLeaseLost, InferenceInvalidResult)):
            prepare(value, result, cancel)
    child.wait.assert_called_once_with(timeout=2)
    assert child.kill.called == (mode in {"timeout", "cancel"})
