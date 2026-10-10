"""Result reference closure shared by AI and Worker; no ORM or numerical imports."""

import json
from uuid import UUID, uuid5

from packages.image_evidence.perspective import CropTransform
from packages.inference_protocol.association import box, select_bottle
from packages.inference_protocol.chemical import context_for_result, extract_entities
from packages.inference_protocol.contract import DOCUMENT
from packages.inference_protocol.fields import extract_fields

RECIPE_KEYS = ("quad", "output_width", "output_height", "transform_version")
EVIDENCE_KEYS = (
    "png_sha256",
    "png_size_bytes",
    "source_rgb_sha256",
    "recognition_rotation_ccw",
    "recognition_width",
    "recognition_height",
    "recognition_rgb_sha256",
)
MAX_CROP_BYTES = 16 * 1024 * 1024


def region_ids(run_id, image_id, index):
    namespace = UUID(run_id)
    return (
        str(uuid5(namespace, f"ocr:{image_id}:{index}")),
        str(uuid5(namespace, f"ocr-crop:{image_id}:{index}")),
    )


def crop_metadata(crop):
    return {"recipe": {key: crop[key] for key in RECIPE_KEYS}, **crop["evidence"]}


def validate_closure(result, image_ids):
    """Schema is checked by caller; all nonempty references must resolve in this result."""

    def reject():
        raise ValueError("Invalid inference evidence closure")

    images = set(image_ids)
    json.dumps(result, allow_nan=False)
    identity = result.get("execution_identity")
    if identity is not None and any(
        identity[key] != result[key]
        for key in (
            "model_bundle_id",
            "model_checksum",
            "dictionary_version_id",
            "dictionary_sha256",
            "pipeline_version",
        )
    ):
        reject()
    if len(images) != len(image_ids) or not images:
        reject()
    quality_ids = [row["image_id"] for row in result["quality"]]
    if len(set(quality_ids)) != len(quality_ids) or set(quality_ids) != images:
        reject()

    def indexed(rows, key):
        values = {row[key]: row for row in rows}
        if len(values) != len(rows) or any(row["image_id"] not in images for row in rows):
            reject()
        return values

    detections = indexed(result["detections"], "detection_id")
    lines = indexed(result["text_regions"], "line_id")
    crops = indexed(result["crops"], "crop_id")
    if len(lines) != len(crops):
        reject()

    def detection_reference(detection_id, image_id):
        if detection_id is not None and (
            detection_id not in detections or detections[detection_id]["image_id"] != image_id
        ):
            reject()

    def bottle_reference(detection_id, image_id):
        detection_reference(detection_id, image_id)
        if detection_id is not None:
            row = detections[detection_id]
            if row["type"] != "bottle" or row["class_id"] != 39:
                reject()

    detection_indices = {}
    for row in result["detections"]:
        box(row["bbox"])
        image = row["image_id"]
        index = detection_indices.get(image, 0)
        detection_indices[image] = index + 1
        if (
            row["type"]
            != DOCUMENT["components"]["schemas"]["Detection"]["properties"]["type"]["enum"][
                row["class_id"]
            ]
        ):
            reject()
        if row["detection_id"] != str(
            uuid5(UUID(result["run_id"]), f"{image}:{row['type']}:{index}")
        ):
            reject()
        detection_reference(row["parent_detection_id"], row["image_id"])
        visited, parent = {row["detection_id"]}, row["parent_detection_id"]
        while parent is not None:
            if parent in visited:
                reject()
            visited.add(parent)
            parent = detections[parent]["parent_detection_id"]
    counters = {}
    for line in result["text_regions"]:
        image = line["image_id"]
        index = counters.get(image, 0)
        counters[image] = index + 1
        if (line["line_id"], line["crop_id"]) != region_ids(result["run_id"], image, index):
            reject()
        bottle_reference(line["detection_id"], image)
        if line["detection_id"] != select_bottle(line["quad"], image, result["detections"]):
            reject()
        crop = crops.get(line["crop_id"])
        if crop is None or any(
            crop[key] != line[key]
            for key in ("line_id", "image_id", "crop_id", "detection_id", "quad")
        ):
            reject()
        recipe = CropTransform.from_dict({key: crop[key] for key in RECIPE_KEYS})
        evidence = crop["evidence"]
        rotated = recipe.output_height / recipe.output_width >= 1.5
        width, height = (
            (recipe.output_height, recipe.output_width)
            if rotated
            else (recipe.output_width, recipe.output_height)
        )
        if (
            evidence["recognition_rotation_ccw"] != (90 if rotated else 0)
            or evidence["recognition_width"] != width
            or evidence["recognition_height"] != height
        ):
            reject()
    # Recompute the complete ordered output, including low-confidence/conflicting
    # fields and both sources. This also rejects omissions and fabricated values.
    context = context_for_result(result)
    dictionary = context[0] if context is not None else None
    if result["ocr_fields"] != extract_fields(result["text_regions"], dictionary):
        reject()
    if context is not None:
        thresholds = context[1]
        if result["entities"] != extract_entities(
            result["ocr_fields"], dictionary, thresholds["ocr_min"], thresholds["entity_min"]
        ):
            reject()
        if result["outcome"] == "facts_ready" and any(
            result[k] for k in ("detections", "text_regions", "ocr_fields", "entities", "relations")
        ):
            reject()
    elif result["entities"]:
        reject()
    for entity in result["entities"]:
        detection_reference(entity["detection_id"], entity["image_id"])
    for relation in result["relations"]:
        for key in ("source_detection_id", "target_detection_id"):
            detection_reference(relation[key], relation["image_id"])
    if result["outcome"] == "needs_retake" and any(
        result[key]
        for key in ("detections", "text_regions", "crops", "ocr_fields", "entities", "relations")
    ):
        reject()


def text_evidence(rows):
    """Serialize existing real OCR rows without inventing field or bottle facts."""
    lines, crops = [], []
    for row in rows:
        evidence = row["crop_evidence"]
        recipe = evidence["recipe"]
        association = row["parent_detection_id"]
        lines.append(
            {
                "line_id": row["line_id"],
                "image_id": row["image_id"],
                "crop_id": evidence["crop_id"],
                "detection_id": association,
                "raw_text": row["text"],
                "confidence": row["confidence"],
                "quad": recipe["quad"],
            }
        )
        crops.append(
            {
                **{key: evidence[key] for key in ("crop_id", "image_id", "line_id")},
                "detection_id": association,
                **recipe,
                "evidence": {key: evidence[key] for key in EVIDENCE_KEYS},
            }
        )
    return lines, crops
