"""Reproducible local real-weight OCR smoke with synthetic label images."""

import argparse
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from apps.ai_inference.ocr_cpu import OcrOptions, run_cpu_ocr
from tools.ml.crop_checks import verify_crops


def main():
    from PIL import Image, ImageDraw, ImageFont

    parser = argparse.ArgumentParser()
    parser.add_argument("--det-dir", type=Path, required=True)
    parser.add_argument("--rec-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    options = OcrOptions(str(args.det_dir.resolve()), str(args.rec_dir.resolve()))
    font = ImageFont.truetype(str(args.font), 42)
    expected = ["ETHANOL", "EXP 2027-12-31", "乙醇"]
    with tempfile.TemporaryDirectory(prefix="labsafe-ocr-smoke-") as directory:
        root = Path(directory)
        paths = [root / name for name in ("blank.png", "label.png")]
        with Image.new("RGB", (800, 400), "white") as image:
            image.save(paths[0])
            draw = ImageDraw.Draw(image)
            for index, text in enumerate(expected):
                draw.text((60, 50 + 100 * index), text, font=font, fill="black")
            image.save(paths[1])
        run_id = "11111111-1111-4111-8111-111111111111"
        first = run_cpu_ocr(options, paths, run_id)
        second = run_cpu_ocr(options, paths, run_id)
        for left, right in zip(first["images"], second["images"]):
            for key in ("image_id", "input_sha256", "width", "height", "lines"):
                if left[key] != right[key]:
                    raise RuntimeError("OCR replay differs")
        if first["images"][0]["lines"]:
            raise RuntimeError("Blank image produced text regions")
        actual = [row["text"] for row in first["images"][1]["lines"]]
        if actual != expected:
            raise RuntimeError(f"Synthetic label differs: {actual!r}")
        if any(row["uncertain"] for row in first["images"][1]["lines"]):
            raise RuntimeError("Synthetic label confidence below the development threshold")
        crops = verify_crops(first, paths)
    evidence = {
        "status": "PASS",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_kind": "synthetic-images",
        "replay_equal": True,
        "crop_reconstruction": crops,
        "font_sha256": hashlib.sha256(args.font.read_bytes()).hexdigest(),
        "font_size": 42,
        "synthetic_ground_truth": expected,
        "result": first,
        "provided_files": {
            role: {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.iterdir()
                if path.is_file()
            }
            for role, directory in (("det", args.det_dir), ("rec", args.rec_dir))
        },
        "limitations": [
            "No real-photo accuracy evaluation",
            "No chemical entity or date facts",
            "No bottle association or HTTP business RPC",
            "No orientation classifier",
            "No Paddle reference comparison or production approval",
        ],
    }
    args.output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Real CPU OCR synthetic-label smoke and replay: PASS")


if __name__ == "__main__":
    main()
