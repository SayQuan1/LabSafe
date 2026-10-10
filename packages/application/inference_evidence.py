"""Transaction-free, sequential source verification, reconstruction and upload."""

from packages.domain.inference_evidence import InferenceDerivative, derivative_key
from packages.image_evidence.analysis import decode_analysis
from packages.image_evidence.perspective import rebuild_ocr_evidence
from packages.inference_protocol.contract import validate
from packages.inference_protocol.evidence import crop_metadata, validate_closure
from packages.inference_protocol.hashing import request_hash


def prepare_evidence(storage, source, result):
    """Runs only in bounded evidence runtime; returns no partial artifact collection."""
    import cv2
    import numpy as np

    cv2.setNumThreads(1)
    validate("InferenceRequest", source)
    validate("InferenceResult", result)
    if source["request_hash"] != request_hash(source):
        raise ValueError("Evidence request hash mismatch")
    for key in (
        "run_id",
        "tenant_id",
        "attempt_id",
        "fencing_token",
        "request_hash",
        "model_bundle_id",
        "model_checksum",
        "dictionary_version_id",
        "dictionary_sha256",
        "pipeline_version",
    ):
        if source[key] != result[key]:
            raise ValueError("Evidence result identity mismatch")
    if result["input_hashes"] != [
        {"image_id": row["image_id"], "sha256": row["sha256"]} for row in source["image_refs"]
    ]:
        raise ValueError("Evidence source hash mismatch")
    validate_closure(result, [row["image_id"] for row in source["image_refs"]])
    artifacts = []
    for reference in source["image_refs"]:
        crops = [crop for crop in result["crops"] if crop["image_id"] == reference["image_id"]]
        if not crops:
            continue
        raw = storage.read_analysis(source["tenant_id"], source["laboratory_id"], reference)
        with decode_analysis(raw, reference["sha256"]) as image:
            rgb = np.asarray(image)
            for crop in crops:
                evidence = rebuild_ocr_evidence(rgb, crop_metadata(crop))
                key = derivative_key(
                    source["tenant_id"],
                    source["laboratory_id"],
                    source["run_id"],
                    crop["crop_id"],
                    evidence.sha256,
                )
                version = storage.put_derivative(key, evidence.png)
                artifacts.append(
                    InferenceDerivative(
                        **{
                            key: crop[key]
                            for key in ("image_id", "crop_id", "line_id", "detection_id")
                        },
                        object_key=key,
                        object_version=version,
                        sha256=evidence.sha256,
                        size_bytes=len(evidence.png),
                    )
                )
        del rgb, raw
    return tuple(artifacts)
