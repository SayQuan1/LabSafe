"""Real CPU models to formal OCR evidence and bounded Worker S3 simulator upload."""

import argparse
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from apps.ai_inference.analysis_cpu import run_analysis_pipeline
from apps.ai_inference.pipeline_cpu import PipelineOptions
from apps.worker.inference_evidence import BoundedEvidencePrepare
from packages.domain.inference_execution import (
    InferenceImage,
    InferenceInput,
    InferenceLease,
    validate_result,
)
from packages.domain.security import ServiceError
from tools.ml.analysis_fixture import payload_for
from tools.ml.evidence_fixture import evidence_store


def worker_environment(directory, endpoint, executable):
    access, secret = Path(directory) / "access", Path(directory) / "secret"
    access.write_text("synthetic", encoding="ascii")
    secret.write_text("test-only", encoding="ascii")
    return {
        "APP_ENV": "test",
        "PUBLIC_ORIGIN": "https://labsafe.test",
        "S3_ENDPOINT": endpoint,
        "S3_PUBLIC_ENDPOINT": "https://labsafe.test",
        "S3_WORKER_ACCESS_KEY_FILE": str(access),
        "S3_WORKER_SECRET_KEY_FILE": str(secret),
        "WORKER_EVIDENCE_PYTHON": str(executable),
    }


def wire_for(source, report):
    # Test-only identity envelope; this does not activate or claim an approved real bundle.
    value = InferenceInput(
        **{
            key: source[key]
            for key in (
                "tenant_id",
                "laboratory_id",
                "item_id",
                "run_id",
                "submission_revision",
                "model_bundle_id",
                "model_checksum",
                "dictionary_version_id",
                "dictionary_sha256",
                "pipeline_version",
                "device_profile",
            )
        },
        images=tuple(InferenceImage(**row) for row in source["image_refs"]),
    )
    lease = InferenceLease(
        value.tenant_id,
        source["item_id"],
        source["item_id"],
        source["attempt_id"],
        source["fencing_token"],
        0,
        1,
        value,
    )
    result = {
        **{
            key: source[key]
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
            )
        },
        "input_hashes": [
            {"image_id": ref["image_id"], "sha256": ref["sha256"]} for ref in source["image_refs"]
        ],
        "outcome": "needs_review",
        "quality": [],
        "detections": [d for row in report["images"] for d in row["detections"]],
        "text_regions": report["text_regions"],
        "crops": report["crops"],
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
    for row in report["images"]:
        quality = row["quality"]
        result["quality"].append(
            {
                "image_id": row["image_id"],
                "status": "pass" if quality["passed"] else "needs_retake",
                **{
                    key: quality[key]
                    for key in ("reasons", "blur_score", "brightness", "glare_ratio")
                },
            }
        )
    validate_result(result, lease)
    return lease, result


def main():
    parser = argparse.ArgumentParser()
    for name in ("model", "det-dir", "rec-dir", "font", "evidence-python", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    os.environ["APP_ENV"] = "test"
    font = ImageFont.truetype(str(args.font), 42)
    expected, raws = ["ETHANOL", "EXP 2027-12-31", "乙醇"], []
    for size in ((800, 400), (400, 800)):
        with Image.new("RGB", size, (180, 180, 180)) as image:
            draw = ImageDraw.Draw(image)
            for index, text in enumerate(expected):
                draw.text((40, 50 + 100 * index), text, font=font, fill="black")
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            raws.append(stream.getvalue())
    source = payload_for(raws)
    with tempfile.TemporaryDirectory(prefix="labsafe-worker-smoke-") as directory:
        with evidence_store(source, raws) as (settings, uploads, seen):
            report = run_analysis_pipeline(
                PipelineOptions(str(args.model), str(args.det_dir), str(args.rec_dir)),
                source,
                settings,
            )
            lease, result = wire_for(source, report)
            assert [line["raw_text"] for line in result["text_regions"]] == expected * 2
            with patch.dict(
                os.environ, worker_environment(directory, settings.endpoint, args.evidence_python)
            ):
                prepare = BoundedEvidencePrepare()
                artifacts = prepare(lease, result, Event())
                replay = prepare(lease, result, Event())
            assert artifacts == replay and len(artifacts) == len(uploads) == 6
            assert all(row.detection_id is None for row in artifacts)
            assert all(
                row.object_version == uploaded["version"] and row.sha256 == uploaded["sha256"]
                for row, uploaded in zip(artifacts, uploads)
            )
            successful_gets = len([row for row in seen if row[0] == "GET"])
        with evidence_store(source, raws, fail_put_at=1) as (settings, failed_uploads, _):
            with patch.dict(
                os.environ, worker_environment(directory, settings.endpoint, args.evidence_python)
            ):
                try:
                    BoundedEvidencePrepare()(lease, result, Event())
                except ServiceError as error:
                    assert error.code == "DEPENDENCY_UNAVAILABLE"
                else:
                    raise AssertionError("Second crop failure returned partial success")
            assert len(failed_uploads) == 1
    output = {
        "status": "PASS",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "inference_kind": "real-official-onnx-cpu",
        "storage_kind": "versioned-loopback-s3-simulator",
        "input_kind": "synthetic-rgb-pngs",
        "images": 2,
        "text_regions": 6,
        "uploaded_crops": 6,
        "worker_replay_exact_versions": True,
        "successful_pinned_gets": successful_gets,
        "second_crop_failure_no_partial_success": True,
        "unreferenced_orphan_on_failure": 1,
        "artifacts": report["artifacts"],
        "runtime": report["runtime"],
        "checks": [
            "formal schema and run/image closure",
            "source RGB/PNG/recognition digests",
            "independent bounded Worker process",
            "exact VersionId and verified reuse",
        ],
        "limitations": [
            "No approved bundle activation or real CPU HTTP service",
            "No live S3/IAM/TLS",
            "No database commit in this smoke; tested separately against MySQL",
            "No field/entity/date/bottle association or field-photo evaluation",
        ],
    }
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {key: output[key] for key in ("status", "images", "text_regions", "uploaded_crops")}
        )
    )


if __name__ == "__main__":
    main()
