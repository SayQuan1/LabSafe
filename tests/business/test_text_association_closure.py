"""Worker recomputes bottle association, including unknown, before network/commit."""

import copy
from uuid import UUID, uuid5

import pytest

from packages.domain.inference_evidence import validate_derivatives
from packages.domain.inference_execution import InferenceInvalidResult, validate_result
from tests.evidence_helpers import add_regions, artifacts_for, lease, result_for


def associated_case():
    value = lease()
    result = add_regions(value, result_for(value, "needs_review"), 1)
    image = value.input.images[0].image_id
    detection_id = str(uuid5(UUID(value.input.run_id), f"{image}:bottle:0"))
    result["detections"] = [
        {
            "detection_id": detection_id,
            "image_id": image,
            "parent_detection_id": None,
            "class_id": 39,
            "type": "bottle",
            "bbox": [0, 0, 1, 1],
            "confidence": 0.1,
        }
    ]
    for row in result["text_regions"] + result["crops"]:
        row["detection_id"] = detection_id
    return value, result


def test_valid_link_preserves_raw_ids_and_derivative_binding():
    value, result = associated_case()
    validate_result(result, value)
    artifacts = artifacts_for(value, result)
    validate_derivatives(value, result, artifacts)
    assert artifacts[0].detection_id == result["detections"][0]["detection_id"]
    assert result["text_regions"][0]["raw_text"] == "乙醇 ETHANOL"


@pytest.mark.parametrize(
    "case",
    [
        "non_bottle",
        "wrong_class",
        "insufficient_overlap",
        "missing_link",
        "line_crop_mismatch",
        "tied_bottles",
        "zero_area",
        "wrong_quad",
    ],
)
def test_worker_rejects_forged_or_inconsistent_association(case):
    value, result = associated_case()
    detection = result["detections"][0]
    if case == "non_bottle":
        detection.update(
            class_id=0,
            type="person",
            detection_id=str(uuid5(UUID(value.input.run_id), f"{detection['image_id']}:person:0")),
        )
        for row in result["text_regions"] + result["crops"]:
            row["detection_id"] = detection["detection_id"]
    elif case == "wrong_class":
        detection["class_id"] = 40
    elif case == "insufficient_overlap":
        detection["bbox"] = [0, 0, 0.79, 1]
    elif case == "missing_link":
        for row in result["text_regions"] + result["crops"]:
            row["detection_id"] = None
    elif case == "line_crop_mismatch":
        result["crops"][0]["detection_id"] = None
    elif case == "tied_bottles":
        other = copy.deepcopy(detection)
        other["detection_id"] = str(
            uuid5(UUID(value.input.run_id), f"{detection['image_id']}:bottle:1")
        )
        result["detections"].append(other)
    elif case == "zero_area":
        detection["bbox"] = [0, 0, 0, 1]
    else:
        for row in result["text_regions"] + result["crops"]:
            row["quad"] = [{"x": 0, "y": 0}] * 4
    with pytest.raises(InferenceInvalidResult):
        validate_result(result, value)


def test_ambiguous_bottles_keep_null_with_complete_evidence():
    value, result = associated_case()
    other = copy.deepcopy(result["detections"][0])
    other["detection_id"] = str(uuid5(UUID(value.input.run_id), f"{other['image_id']}:bottle:1"))
    result["detections"].append(other)
    for row in result["text_regions"] + result["crops"]:
        row["detection_id"] = None
    validate_result(result, value)
    assert result["outcome"] == "needs_review"
    assert not result["ocr_fields"] and not result["entities"]
