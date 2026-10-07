"""Bounded local CPU detector. No HTTP image references or business writes."""

import hashlib
import io
import json
import math
import multiprocessing
import os
import platform
import sys
import time
import warnings
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from uuid import UUID, uuid5

from apps.ai_inference.adapters.dfine import ADAPTER_ID, MAX_DETECTIONS, AdapterError, OnnxDetector

MAX_IMAGE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class CpuOptions:
    model_path: str
    config_path: str
    preprocessor_path: str
    detection_min: float = 0.4
    threads: int = 2
    timeout_seconds: float = 180.0

    def validate(self):
        if type(self.detection_min) not in (int, float) or not math.isfinite(self.detection_min):
            raise AdapterError("VALIDATION_ERROR", "Invalid detection threshold")
        if not 0 <= self.detection_min <= 1:
            raise AdapterError("VALIDATION_ERROR", "Invalid detection threshold")
        if type(self.threads) is not int or not 1 <= self.threads <= 16:
            raise AdapterError("VALIDATION_ERROR", "CPU threads must be 1..16")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(
            self.timeout_seconds
        ):
            raise AdapterError("VALIDATION_ERROR", "Invalid CPU deadline")
        if not 0 < self.timeout_seconds <= 180:
            raise AdapterError("VALIDATION_ERROR", "CPU deadline must be within 180 seconds")


def _decode_local(path):
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_IMAGE_BYTES + 1)
    except OSError as error:
        raise AdapterError("IMAGE_INVALID", "Local image cannot be read") from error
    if len(raw) > MAX_IMAGE_BYTES:
        raise AdapterError("IMAGE_TOO_LARGE", "Local image exceeds the byte limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"PNG", "JPEG", "WEBP"}:
                    raise AdapterError("UNSUPPORTED_MEDIA_TYPE", "Unsupported local image format")
                if getattr(source, "n_frames", 1) != 1:
                    raise AdapterError("IMAGE_INVALID", "Animated images are not supported")
                width, height = source.size
                if max(width, height) > 10000 or width * height > 40_000_000:
                    raise AdapterError("IMAGE_TOO_LARGE", "Local image dimensions exceed the limit")
                source.load()
                # The CLI accepts original local images. Worker analysis PNGs have
                # already lost EXIF, so this cannot rotate them a second time.
                image = ImageOps.exif_transpose(source).convert("RGB")
    except AdapterError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise AdapterError("IMAGE_TOO_LARGE", "Local image exceeds the pixel limit") from error
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as error:
        raise AdapterError("IMAGE_INVALID", "Local image is invalid or truncated") from error
    return image, hashlib.sha256(raw).hexdigest()


def _compute(options, paths, run_id):
    """Executed only in a spawned child; load the model once for all images."""
    from PIL import Image

    started = time.monotonic()
    detector = OnnxDetector.from_path(
        options.model_path,
        providers=["CPUExecutionProvider"],
        config_path=options.config_path,
        preprocessor_path=options.preprocessor_path,
        cpu_threads=options.threads,
    )
    if detector.providers != ("CPUExecutionProvider",):
        raise AdapterError("MODEL_VERSION_UNAVAILABLE", "CPU-only session is required")
    smoke = Image.new("RGB", (640, 640))
    try:
        detector.detect(smoke, options.detection_min)
    finally:
        smoke.close()
    load_ms = round((time.monotonic() - started) * 1000)
    evidence = detector.evidence
    runtime = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {name: version(name) for name in ("onnxruntime", "numpy", "Pillow")},
        "providers": list(detector.providers),
        "intra_op_threads": options.threads,
        "inter_op_threads": 1,
        "execution_mode": "sequential",
        "preprocess": "rgb-stretch640-bilinear-rescale255",
        "postprocess": "qmax-sigmoid-threshold-geometry-no-nms",
    }
    runtime_sha = hashlib.sha256(
        json.dumps(runtime, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    images, total = [], 0
    for ordinal, path in enumerate(paths):
        image, digest = _decode_local(path)
        try:
            detected_at = time.monotonic()
            diagnostics = {}
            rows = detector.detect(image, options.detection_min, diagnostics=diagnostics)
            detection_ms = round((time.monotonic() - detected_at) * 1000)
            total += len(rows)
            if total > MAX_DETECTIONS:
                raise AdapterError("MODEL_ERROR", "All images exceed the 100-detection limit")
            image_id = str(uuid5(UUID(run_id), f"image:{ordinal}:{digest}"))
            detections = [
                {
                    "detection_id": str(uuid5(UUID(run_id), f"{image_id}:{row['type']}:{index}")),
                    "image_id": image_id,
                    "parent_detection_id": None,
                    **{key: row[key] for key in ("class_id", "type", "bbox", "confidence")},
                }
                for index, row in enumerate(rows)
            ]
            images.append(
                {
                    "image_id": image_id,
                    "input_sha256": digest,
                    "width": image.width,
                    "height": image.height,
                    "diagnostics": diagnostics,
                    "detection_ms": detection_ms,
                    "detections": detections,
                }
            )
        finally:
            image.close()
    return {
        "schema_version": "coco80-local-v1",
        "run_id": run_id,
        "purpose": "development",
        "is_simulated": False,
        "device": "cpu",
        "adapter_id": ADAPTER_ID,
        "model_sha256": evidence.model_sha256,
        "config_sha256": evidence.config_sha256,
        "preprocessor_sha256": evidence.preprocessor_sha256,
        "runtime": runtime,
        "runtime_sha256": runtime_sha,
        "detection_min": options.detection_min,
        "review_required": True,
        "business_capabilities": {
            "ocr": False,
            "chemical_entities": False,
            "container_relations": False,
        },
        "load_and_smoke_ms": load_ms,
        "total_ms": round((time.monotonic() - started) * 1000),
        "images": images,
    }


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


def run_cpu_detection(options: CpuOptions, paths, run_id: str):
    return run_cpu_workload(options, paths, run_id, _child)


def run_cpu_workload(options, paths, run_id: str, target):
    """One CPU child, one total deadline, no partial result after failure."""
    if sys.version_info[:2] != (3, 11):
        raise AdapterError("VALIDATION_ERROR", "CPU runtime requires Python 3.11")
    if os.environ.get("APP_ENV") not in {"dev", "test"}:
        raise AdapterError("VALIDATION_ERROR", "Local CPU inference requires APP_ENV=dev or test")
    options.validate()
    try:
        run_id = str(UUID(run_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise AdapterError("VALIDATION_ERROR", "Invalid run ID") from error
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 3:
        raise AdapterError("VALIDATION_ERROR", "Supply 1..3 local images")
    paths = tuple(str(Path(path).resolve()) for path in paths)
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    child = context.Process(target=target, args=(send, options, paths, run_id), daemon=True)
    deadline = time.monotonic() + options.timeout_seconds
    try:
        child.start()
        send.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError("AI_TIMEOUT", "CPU inference deadline exceeded")
            if receive.poll(min(remaining, 0.1)):
                try:
                    kind, payload = receive.recv()
                except (EOFError, OSError) as error:
                    raise AdapterError(
                        "MODEL_ERROR", "CPU child exited without a result"
                    ) from error
                if time.monotonic() >= deadline:
                    raise AdapterError("AI_TIMEOUT", "CPU inference deadline exceeded")
                if kind == "result" and isinstance(payload, dict):
                    return payload
                if kind == "error" and isinstance(payload, str):
                    raise AdapterError(payload, "CPU inference failed")
                raise AdapterError("MODEL_ERROR", "Invalid CPU child response")
            if not child.is_alive():
                raise AdapterError("MODEL_ERROR", "CPU child exited without a result")
    finally:
        send.close()
        receive.close()
        if child.pid is not None:
            child.join(timeout=0.2)
            if child.is_alive():
                child.kill()
                child.join(timeout=2)
            if child.is_alive():
                raise AdapterError("AI_TIMEOUT", "CPU child could not be reclaimed")
            child.close()
