"""Repeatable synthetic-image smoke using a real, locally supplied official ONNX."""

import argparse
import json
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import distributions
from pathlib import Path
from uuid import UUID

from apps.ai_inference.cpu import CpuOptions, run_cpu_detection


def main():
    import numpy as np
    from PIL import Image

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = args.model.resolve()
    options = CpuOptions(
        str(model),
        str(model.with_name("config.json")),
        str(model.with_name("preprocessor_config.json")),
    )
    run_id = str(UUID("11111111-1111-4111-8111-111111111111"))
    with tempfile.TemporaryDirectory(prefix="labsafe-cpu-smoke-") as directory:
        root = Path(directory)
        paths = [root / name for name in ("blank.png", "portrait.png", "noise.png")]
        with Image.new("RGB", (800, 400)) as image:
            image.save(paths[0])
        pixels = np.zeros((800, 400, 3), dtype=np.uint8)
        pixels[:, :200, 0] = 255
        pixels[:, 200:, 2] = 255
        with Image.fromarray(pixels) as image:
            image.save(paths[1])
        pixels = np.random.default_rng(20261007).integers(0, 256, (400, 800, 3), dtype=np.uint8)
        with Image.fromarray(pixels) as image:
            image.save(paths[2])
        started = time.monotonic()
        first = run_cpu_detection(options, paths, run_id)
        wall_ms = round((time.monotonic() - started) * 1000)
        second = run_cpu_detection(options, paths, run_id)
        for left, right in zip(first["images"], second["images"]):
            for key in ("image_id", "input_sha256", "width", "height", "diagnostics", "detections"):
                if left[key] != right[key]:
                    raise RuntimeError("CPU smoke replay differs")
            if left["diagnostics"]["candidates"] != 300:
                raise RuntimeError("CPU smoke candidate count differs")
        if first["runtime"]["providers"] != ["CPUExecutionProvider"]:
            raise RuntimeError("Unexpected CPU provider")
    evidence = {
        "status": "PASS",
        "executed_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_kind": "synthetic-images",
        "bounded_process_wall_ms": wall_ms,
        "replay_equal": True,
        "installed_packages": dict(
            sorted((d.metadata["Name"], d.version) for d in distributions())
        ),
        "result": first,
        "limitations": [
            "No real-photo accuracy evaluation",
            "No OCR or business RPC",
            "No CUDA evaluation",
            "Not a production runtime lock or performance approval",
        ],
    }
    args.output.write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    print("CPU model smoke and replay: PASS")


if __name__ == "__main__":
    main()
