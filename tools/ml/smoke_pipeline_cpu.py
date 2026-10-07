"""Real joint CPU model smoke with strict quality gating and reproducible inputs."""

import argparse
import hashlib
import json
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from apps.ai_inference.pipeline_cpu import PipelineOptions, run_cpu_pipeline
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
    options = PipelineOptions(
        str(args.model.resolve()), str(args.det_dir.resolve()), str(args.rec_dir.resolve())
    )
    font = ImageFont.truetype(str(args.font), 42)
    expected = ["ETHANOL", "EXP 2027-12-31", "乙醇"]
    run_id = "11111111-1111-4111-8111-111111111111"
    with tempfile.TemporaryDirectory(prefix="labsafe-pipeline-smoke-") as directory:
        root = Path(directory)
        paths = [root / name for name in ("landscape.png", "portrait.png")]
        for path, size in zip(paths, ((800, 400), (400, 800))):
            with Image.new("RGB", size, (180, 180, 180)) as image:
                draw = ImageDraw.Draw(image)
                for index, text in enumerate(expected):
                    draw.text((40, 50 + 100 * index), text, font=font, fill="black")
                image.save(path)
        rotated = root / "rotated-clockwise90.png"
        with Image.open(paths[0]) as image, image.transpose(Image.Transpose.ROTATE_270) as turned:
            turned.save(rotated)
        paths.append(rotated)
        blank = root / "black.png"
        with Image.new("RGB", (800, 400)) as image:
            image.save(blank)
        first = run_cpu_pipeline(options, paths, run_id)
        second = run_cpu_pipeline(options, paths, run_id)
        if first["outcome"] != "needs_review" or not first["models_executed"]:
            raise RuntimeError("Clean synthetic images did not pass the real quality gate")
        for index, (left, right) in enumerate(zip(first["images"], second["images"])):
            for key in (
                "image_id",
                "input_sha256",
                "quality",
                "detections",
                "lines",
                "detection_diagnostics",
                "inference_executed",
            ):
                if left[key] != right[key]:
                    raise RuntimeError("Joint CPU replay differs")
            actual = [row["text"] for row in left["lines"]]
            if actual != expected if index < 2 else sorted(actual) != sorted(expected):
                raise RuntimeError("Synthetic OCR label differs")
            if any(row["uncertain"] for row in left["lines"]):
                raise RuntimeError("Synthetic OCR confidence below development threshold")
            if left["detection_diagnostics"]["candidates"] != 300:
                raise RuntimeError("Detector candidate count differs")
        crops = verify_crops(first, paths)
        if crops["count"] != 9 or crops["rotated_count"] != 3:
            raise RuntimeError("Expected nine crops including three rotated recognition regions")
        mixed = run_cpu_pipeline(options, [paths[0], blank], run_id)
        if mixed["outcome"] != "needs_retake" or mixed["models_executed"] or mixed["artifacts"]:
            raise RuntimeError("Failed-image whole-batch gate did not stop model loading")
        if any(
            row["inference_executed"] or row["detections"] or row["lines"]
            for row in mixed["images"]
        ):
            raise RuntimeError("Mixed batch leaked partial detections or OCR")
        quality = run_cpu_pipeline(replace(options, quality_only=True), paths, run_id)
        if quality["outcome"] != "quality_passed" or quality["models_executed"]:
            raise RuntimeError("Quality-only branch ran model inference")
        if [row["quality"] for row in quality["images"]] != [
            row["quality"] for row in first["images"]
        ]:
            raise RuntimeError("Quality-only/joint quality scores differ")
    evidence = {
        "status": "PASS",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_kind": "synthetic-images",
        "replay_equal": True,
        "quality_stage_equal": True,
        "crop_reconstruction": crops,
        "font_sha256": hashlib.sha256(args.font.read_bytes()).hexdigest(),
        "generation": {
            "font_size": 42,
            "background_rgb": [180, 180, 180],
            "origins": [[40, 50], [40, 150], [40, 250]],
            "ground_truth": expected,
            "image_sizes": [[800, 400], [400, 800], [400, 800]],
            "third_image_rotation": "clockwise90-from-first-image",
        },
        "joint_result": first,
        "mixed_retake_result": mixed,
        "quality_only_result": quality,
        "limitations": [
            "Synthetic engineering smoke, not real-photo accuracy",
            "No chemical/date facts or bottle association",
            "No HTTP/Worker RPC",
            "No production runtime lock, performance approval or CUDA evaluation",
        ],
    }
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print("Real CPU quality + COCO80 + OCR joint smoke, batch gate and replay: PASS")


if __name__ == "__main__":
    main()
