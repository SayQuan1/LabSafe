"""Real CPU models with simulated fixed-version S3 input, replay and crop checks."""

import argparse
import io
import json
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.analysis_cpu import run_analysis_pipeline
from apps.ai_inference.pipeline_cpu import PipelineOptions, run_cpu_pipeline
from tools.ml.analysis_fixture import object_server, payload_for
from tools.ml.crop_checks import verify_crops


def main():
    from PIL import Image, ImageDraw, ImageFont

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--det-dir", type=Path, required=True)
    parser.add_argument("--rec-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    options = PipelineOptions(str(args.model), str(args.det_dir), str(args.rec_dir))
    font = ImageFont.truetype(str(args.font), 42)
    expected = ["ETHANOL", "EXP 2027-12-31", "乙醇"]
    raws = []
    for size in ((800, 400), (400, 800)):
        with Image.new("RGB", size, (180, 180, 180)) as image:
            draw = ImageDraw.Draw(image)
            for index, label in enumerate(expected):
                draw.text((40, 50 + 100 * index), label, font=font, fill="black")
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            raws.append(stream.getvalue())
    payload = payload_for(raws)
    with object_server(payload, raws) as (settings, seen):
        first = run_analysis_pipeline(options, payload, settings)
        second = run_analysis_pipeline(options, payload, settings)
        quality = run_analysis_pipeline(replace(options, quality_only=True), payload, settings)
        if len(seen) != 6:
            raise RuntimeError("Expected exactly six versioned GET requests")
    if first["outcome"] != "needs_review" or not first["models_executed"]:
        raise RuntimeError("Joint real CPU models did not run")
    for left, right, ref in zip(first["images"], second["images"], payload["image_refs"]):
        for key in ("image_id", "input_sha256", "object_version", "quality", "detections", "lines"):
            if left[key] != right[key]:
                raise RuntimeError("Remote CPU replay differs")
        if left["image_id"] != ref["image_id"] or left["object_version"] != ref["object_version"]:
            raise RuntimeError("Worker input identity was changed")
        if [line["text"] for line in left["lines"]] != expected:
            raise RuntimeError("Synthetic real OCR differs")
    with tempfile.TemporaryDirectory(prefix="labsafe-analysis-smoke-") as directory:
        paths = [Path(directory) / f"{index}.png" for index in range(len(raws))]
        for path, raw in zip(paths, raws):
            path.write_bytes(raw)
        local = run_cpu_pipeline(replace(options, quality_only=True), paths, payload["run_id"])
        scores = [row["quality"] for row in first["images"]]
        if scores != [row["quality"] for row in quality["images"]] or scores != [
            row["quality"] for row in local["images"]
        ]:
            raise RuntimeError("Local/analysis/quality-only scores differ")
        crops = verify_crops(first, paths)
    with object_server(payload, raws, headers={"x-amz-version-id": "wrong-version"}) as (
        settings,
        seen,
    ):
        try:
            run_analysis_pipeline(options, payload, settings)
        except AdapterError as error:
            if error.code != "HASH_MISMATCH":
                raise
        else:
            raise RuntimeError("Wrong object version was accepted")
        if len(seen) != 1:
            raise RuntimeError("Wrong-version run continued downloading")
    stream = io.BytesIO()
    with Image.new("RGB", (800, 400)) as image:
        image.save(stream, format="PNG")
    mixed_raws = [raws[0], stream.getvalue()]
    mixed_payload = payload_for(mixed_raws)
    with object_server(mixed_payload, mixed_raws) as (settings, _):
        mixed = run_analysis_pipeline(options, mixed_payload, settings)
    if mixed["outcome"] != "needs_retake" or mixed["models_executed"] or mixed["artifacts"]:
        raise RuntimeError("Retake batch loaded models")
    evidence = {
        "status": "PASS",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_kind": "synthetic-rgb-pngs",
        "storage_kind": "loopback-s3-simulator",
        "inference_kind": "real-official-onnx-cpu",
        "versioned_get_count": 6,
        "replay_equal": True,
        "worker_image_ids_preserved": True,
        "quality_local_analysis_equal": True,
        "wrong_version_rejected": True,
        "crop_reconstruction": crops,
        "joint_result": first,
        "quality_only_result": quality,
        "mixed_retake_result": mixed,
        "limitations": [
            "No real object-store IAM/TLS acceptance",
            "No real-photo accuracy evaluation",
            "No HTTP/Worker RPC or ready model approval",
            "No chemical/date facts or association",
        ],
    }
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print("Versioned analysis GET + real CPU models + stable IDs/replay/crops: PASS")


if __name__ == "__main__":
    main()
