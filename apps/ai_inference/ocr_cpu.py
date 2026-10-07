"""Bounded local OCR report, independent of chemical facts and the HTTP fixture."""

import hashlib
import json
import platform
import time
from dataclasses import dataclass
from importlib.metadata import version
from uuid import UUID, uuid5

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.adapters.ocrv6 import ADAPTER_ID, ARTIFACTS, MAX_REGIONS, OnnxOCR
from apps.ai_inference.cpu import CpuOptions, _decode_local, run_cpu_workload
from packages.image_evidence.perspective import crop_runtime


@dataclass(frozen=True)
class OcrOptions:
    det_dir: str
    rec_dir: str
    text_min: float = 0.6
    threads: int = 2
    timeout_seconds: float = 180.0

    def validate(self):
        CpuOptions("", "", "", self.text_min, self.threads, self.timeout_seconds).validate()


def _compute(options, paths, run_id):
    started = time.monotonic()
    ocr = OnnxOCR.from_directories(options.det_dir, options.rec_dir, options.threads)
    ocr.smoke()
    loaded_ms = round((time.monotonic() - started) * 1000)
    images, total = [], 0
    for ordinal, path in enumerate(paths):
        image, digest = _decode_local(path)
        try:
            inferred_at = time.monotonic()
            rows = ocr.recognize(image)
            total += len(rows)
            if total > MAX_REGIONS:
                raise AdapterError("MODEL_ERROR", "All images exceed the OCR region capacity")
            image_id = str(uuid5(UUID(run_id), f"image:{ordinal}:{digest}"))
            annotate_lines(rows, run_id, image_id, options.text_min)
            images.append(
                {
                    "image_id": image_id,
                    "input_sha256": digest,
                    "width": image.width,
                    "height": image.height,
                    "ocr_ms": round((time.monotonic() - inferred_at) * 1000),
                    "lines": rows,
                }
            )
        finally:
            image.close()
    runtime = ocr_runtime(options.threads)
    return {
        "schema_version": "ocrv6-local-v2",
        "run_id": run_id,
        "purpose": "development",
        "is_simulated": False,
        "device": "cpu",
        "adapter_id": ADAPTER_ID,
        "artifacts": ARTIFACTS,
        "runtime": runtime,
        "runtime_sha256": hashlib.sha256(
            json.dumps(runtime, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest(),
        "text_min": options.text_min,
        "review_required": True,
        "business_capabilities": {
            "text_recognition": True,
            "chemical_entities": False,
            "container_relations": False,
            "bottle_association": False,
        },
        "load_and_smoke_ms": loaded_ms,
        "total_ms": round((time.monotonic() - started) * 1000),
        "images": images,
    }


def annotate_lines(rows, run_id, image_id, text_min):
    for index, row in enumerate(rows):
        row.update(
            line_id=str(uuid5(UUID(run_id), f"ocr:{image_id}:{index}")),
            image_id=image_id,
            parent_detection_id=None,
            uncertain=not row["text"] or row["confidence"] < text_min,
        )
        row["crop_evidence"].update(
            crop_id=str(uuid5(UUID(run_id), f"ocr-crop:{image_id}:{index}")),
            image_id=image_id,
            line_id=row["line_id"],
        )


def ocr_runtime(threads):
    runtime = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {
            name: version(name)
            for name in (
                "onnxruntime",
                "numpy",
                "Pillow",
                "opencv-python-headless",
                "pyclipper",
                "PyYAML",
            )
        },
        "providers": ["CPUExecutionProvider"],
        "intra_op_threads": threads,
        "inter_op_threads": 1,
        "execution_mode": "sequential",
        "det_preprocess": "bgr-imagenet-max960-round32-v1",
        "det_postprocess": "db-thresh0.2-box0.45-unclip1.4-no-dilation-v1",
        "rec_preprocess": "bgr-height48-minwidth320-align8-max3200-pad0-v1",
        "rec_postprocess": "ctc-blank0-deduplicate-append-space-v1",
        "crop": crop_runtime(),
        "orientation_classifier": False,
    }
    return runtime


def _child(channel, options, paths, run_id):
    try:
        channel.send(("result", _compute(options, paths, run_id)))
    except AdapterError as error:
        channel.send(("error", error.code))
    except MemoryError:
        channel.send(("error", "MODEL_OOM"))
    except Exception:
        channel.send(("error", "MODEL_ERROR"))
    finally:
        channel.close()


def run_cpu_ocr(options, paths, run_id):
    return run_cpu_workload(options, paths, run_id, _child)
