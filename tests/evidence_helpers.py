"""Synthetic wire-only OCR evidence; numerical tests replace the claimed digests."""

import hashlib
import json
from dataclasses import replace
from uuid import UUID, uuid4, uuid5

from packages.domain.inference_evidence import InferenceDerivative, derivative_key
from packages.domain.inference_execution import InferenceImage, InferenceInput, InferenceLease
from packages.inference_protocol.evidence import region_ids


def add_regions(lease, result, count=2):
    image_id = lease.input.images[0].image_id
    for index in range(count):
        line_id, crop_id = region_ids(lease.input.run_id, image_id, index)
        quad = [{"x": x, "y": y} for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]
        result["text_regions"].append(
            {
                "line_id": line_id,
                "crop_id": crop_id,
                "image_id": image_id,
                "detection_id": None,
                "raw_text": "乙醇 ETHANOL",
                "confidence": 0.9,
                "quad": quad,
            }
        )
        result["crops"].append(
            {
                "line_id": line_id,
                "crop_id": crop_id,
                "image_id": image_id,
                "detection_id": None,
                "quad": quad,
                "output_width": 20,
                "output_height": 10,
                "transform_version": "perspective-rgb-v1",
                "evidence": {
                    "png_sha256": "d" * 64,
                    "png_size_bytes": 100,
                    "source_rgb_sha256": "e" * 64,
                    "recognition_rotation_ccw": 0,
                    "recognition_width": 20,
                    "recognition_height": 10,
                    "recognition_rgb_sha256": "e" * 64,
                },
            }
        )
    return result


def artifacts_for(lease, result):
    return tuple(
        InferenceDerivative(
            **{key: crop[key] for key in ("image_id", "crop_id", "line_id", "detection_id")},
            object_key=derivative_key(
                lease.tenant_id,
                lease.input.laboratory_id,
                lease.input.run_id,
                crop["crop_id"],
                crop["evidence"]["png_sha256"],
            ),
            object_version="synthetic-crop/v1+exact=",
            sha256=crop["evidence"]["png_sha256"],
            size_bytes=crop["evidence"]["png_size_bytes"],
        )
        for crop in result["crops"]
    )


def add_bottles(value, result, count=1):
    """Synthetic full-image boxes for association/transaction tests, never model evidence."""
    image = value.input.images[0].image_id
    result["detections"] = [
        {
            "detection_id": str(uuid5(UUID(value.input.run_id), f"{image}:bottle:{index}")),
            "image_id": image,
            "parent_detection_id": None,
            "class_id": 39,
            "type": "bottle",
            "bbox": [0, 0, 1, 1],
            "confidence": 0.9,
        }
        for index in range(count)
    ]
    for row in result["text_regions"] + result["crops"]:
        row["detection_id"] = result["detections"][0]["detection_id"] if count == 1 else None
    return result


def add_chemical_context(result, entries=()):
    """Explicit synthetic dictionary/bundle bytes, shared by spawn and MySQL tests."""
    from packages.inference_protocol.chemical import RUNTIME_PROFILE, extract_entities
    from packages.inference_protocol.fields import extract_fields
    from tests.ai.test_supervisor import IDENTITY

    data = {
        "purpose": "development",
        "dictionary_version_id": result["dictionary_version_id"],
        "entries": list(entries),
    }
    raw = json.dumps(data, ensure_ascii=False)
    result["dictionary_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    bundle = {
        "purpose": "development",
        "bundle_id": result["model_bundle_id"],
        "dictionary_version_id": result["dictionary_version_id"],
        "pipeline_version": "vision-v1",
        "device_profile": "cpu",
        "adapter_id": "dfine-coco80-rgb-stretch-v1",
        "runtime_profile": RUNTIME_PROFILE,
        "artifacts": {
            name: {
                "path": "C:/synthetic/" + name,
                "sha256": result["dictionary_sha256"] if name == "dictionary" else "a" * 64,
            }
            for name in (
                "detector_model",
                "detector_config",
                "detector_preprocessor",
                "ocr_det_model",
                "ocr_det_config",
                "ocr_rec_model",
                "ocr_rec_config",
                "dictionary",
                "runtime_lock",
            )
        },
        "thresholds": {
            "detection_min": 0.4,
            "ocr_min": 0.6,
            "entity_min": 0.9,
            "blur_min": 80,
            "dark_min": 0.12,
            "glare_max": 0.3,
        },
        "threads": 2,
    }
    bundle_raw = json.dumps(bundle)
    result["model_checksum"] = hashlib.sha256(bundle_raw.encode()).hexdigest()
    result["extraction_context"] = {"bundle_json": bundle_raw, "dictionary_json": raw}
    result["execution_identity"] = dict(
        IDENTITY,
        runtime_profile=RUNTIME_PROFILE,
        **{
            k: result[k]
            for k in (
                "model_bundle_id",
                "model_checksum",
                "dictionary_version_id",
                "dictionary_sha256",
                "pipeline_version",
            )
        },
    )
    result["ocr_fields"] = extract_fields(result["text_regions"], data)
    result["entities"] = extract_entities(result["ocr_fields"], data)
    return result


def synthetic_chemical_entries():
    return [
        {
            "entity_id": "33333333-3333-4333-8333-333333333333",
            "canonical_name": "Alpha",
            "aliases": ["甲"],
            "cas": "64-17-5",
            "source": "synthetic test only; no safety data",
        }
    ]


def with_source_hash(lease, sha):
    image = lease.input.images[0]
    image = replace(image, sha256=sha, object_key=image.object_key.replace(image.sha256, sha))
    return replace(lease, input=replace(lease.input, images=(image,)))


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
                object_version="analysis-v1",
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
        "text_regions": [],
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
