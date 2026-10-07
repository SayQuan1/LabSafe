"""Local quality gate and joint COCO-80/OCR execution in one bounded CPU child."""

import hashlib
import json
import platform
import time
from dataclasses import asdict, dataclass
from uuid import UUID, uuid5

from apps.ai_inference.adapters.dfine import ADAPTER_ID, MAX_DETECTIONS, AdapterError, OnnxDetector
from apps.ai_inference.adapters.ocrv6 import ARTIFACTS, MAX_REGIONS, OnnxOCR
from apps.ai_inference.adapters.quality import QualityThresholds, evaluate_quality, quality_runtime
from apps.ai_inference.cpu import CpuOptions, _decode_local, run_cpu_workload
from apps.ai_inference.ocr_cpu import OcrOptions, annotate_lines, ocr_runtime

PIPELINE_ID = "quality-coco80-ocrv6-cpu-v2"


@dataclass(frozen=True)
class PipelineOptions:
    model_path: str | None = None
    det_dir: str | None = None
    rec_dir: str | None = None
    config_path: str | None = None
    preprocessor_path: str | None = None
    detection_min: float = 0.4
    text_min: float = 0.6
    quality: QualityThresholds = QualityThresholds()
    quality_only: bool = False
    threads: int = 2
    timeout_seconds: float = 180.0

    def validate(self):
        CpuOptions("", "", "", self.detection_min, self.threads, self.timeout_seconds).validate()
        OcrOptions("", "", self.text_min, self.threads, self.timeout_seconds).validate()
        if type(self.quality_only) is not bool or not isinstance(self.quality, QualityThresholds):
            raise AdapterError("VALIDATION_ERROR", "Invalid pipeline configuration")
        self.quality.validate()
        if not self.quality_only and any(
            not isinstance(p, str) or not p for p in (self.model_path, self.det_dir, self.rec_dir)
        ):
            raise AdapterError("VALIDATION_ERROR", "Detector and OCR paths are required")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _compute(options, paths, run_id):
    """Decode once, gate the whole batch, then load each session once."""
    from PIL import Image

    started = time.monotonic()
    decoded, images = [], []
    quality_ms = 0
    try:
        for ordinal, path in enumerate(paths):
            image, digest = _decode_local(path)
            decoded.append(image)
            checked_at = time.monotonic()
            quality = evaluate_quality(image, options.quality)
            quality_ms += round((time.monotonic() - checked_at) * 1000)
            image_id = str(uuid5(UUID(run_id), f"image:{ordinal}:{digest}"))
            images.append(
                {
                    "image_id": image_id,
                    "input_sha256": digest,
                    "width": image.width,
                    "height": image.height,
                    "quality": quality,
                    "inference_executed": False,
                    "detections": [],
                    "lines": [],
                    "detection_ms": 0,
                    "ocr_ms": 0,
                }
            )
        passed = all(row["quality"]["passed"] for row in images)
        runtime = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "quality": quality_runtime(),
            "detector": None,
            "ocr": None,
        }
        artifacts, loaded_ms = {}, 0
        models_executed = passed and not options.quality_only
        if models_executed:
            loaded_at = time.monotonic()
            detector = OnnxDetector.from_path(
                options.model_path,
                providers=["CPUExecutionProvider"],
                config_path=options.config_path,
                preprocessor_path=options.preprocessor_path,
                cpu_threads=options.threads,
            )
            if detector.providers != ("CPUExecutionProvider",):
                raise AdapterError("MODEL_VERSION_UNAVAILABLE", "CPU-only detector required")
            with Image.new("RGB", (640, 640)) as smoke:
                detector.detect(smoke, options.detection_min)
            ocr = OnnxOCR.from_directories(options.det_dir, options.rec_dir, options.threads)
            ocr.smoke()
            loaded_ms = round((time.monotonic() - loaded_at) * 1000)
            evidence = detector.evidence
            artifacts = {
                "detector": {
                    "model_sha256": evidence.model_sha256,
                    "config_sha256": evidence.config_sha256,
                    "preprocessor_sha256": evidence.preprocessor_sha256,
                },
                "ocr": ARTIFACTS,
            }
            runtime["detector"] = {
                "adapter_id": ADAPTER_ID,
                "providers": list(detector.providers),
                "intra_op_threads": options.threads,
                "inter_op_threads": 1,
                "execution_mode": "sequential",
                "preprocess": "rgb-stretch640-bilinear-rescale255",
                "postprocess": "qmax-sigmoid-threshold-geometry-no-nms",
            }
            runtime["ocr"] = ocr_runtime(options.threads)
            detections_total, lines_total = 0, 0
            for image, row in zip(decoded, images):
                inferred_at = time.monotonic()
                diagnostics = {}
                detections = detector.detect(image, options.detection_min, diagnostics=diagnostics)
                row["detection_ms"] = round((time.monotonic() - inferred_at) * 1000)
                detections_total += len(detections)
                if detections_total > MAX_DETECTIONS:
                    raise AdapterError("MODEL_ERROR", "Pipeline detection capacity exceeded")
                inferred_at = time.monotonic()
                lines = ocr.recognize(image)
                row["ocr_ms"] = round((time.monotonic() - inferred_at) * 1000)
                lines_total += len(lines)
                if lines_total > MAX_REGIONS:
                    raise AdapterError("MODEL_ERROR", "Pipeline OCR region capacity exceeded")
                row["detections"] = [
                    {
                        "detection_id": str(
                            uuid5(UUID(run_id), f"{row['image_id']}:{d['type']}:{index}")
                        ),
                        "image_id": row["image_id"],
                        "parent_detection_id": None,
                        **{key: d[key] for key in ("class_id", "type", "bbox", "confidence")},
                    }
                    for index, d in enumerate(detections)
                ]
                annotate_lines(lines, run_id, row["image_id"], options.text_min)
                row.update(inference_executed=True, lines=lines, detection_diagnostics=diagnostics)
        config = {
            "pipeline_id": PIPELINE_ID,
            "detection_min": options.detection_min,
            "text_min": options.text_min,
            "quality": asdict(options.quality),
            "quality_only": options.quality_only,
        }
        return {
            "schema_version": "cpu-pipeline-local-v2",
            "pipeline_id": PIPELINE_ID,
            "run_id": run_id,
            "purpose": "development",
            "is_simulated": False,
            "device": "cpu",
            "outcome": "needs_retake"
            if not passed
            else ("quality_passed" if options.quality_only else "needs_review"),
            "review_required": True,
            "models_executed": models_executed,
            "artifacts": artifacts,
            "runtime": runtime,
            "runtime_sha256": _digest(runtime),
            "config": config,
            "config_sha256": _digest(config),
            "business_capabilities": {
                "chemical_entities": False,
                "container_relations": False,
                "bottle_association": False,
                "date_facts": False,
            },
            "timing_ms": {
                "quality_ms": quality_ms,
                "load_and_smoke_ms": loaded_ms,
                "total_ms": round((time.monotonic() - started) * 1000),
            },
            "images": images,
        }
    finally:
        for image in decoded:
            image.close()


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


def run_cpu_pipeline(options, paths, run_id):
    return run_cpu_workload(options, paths, run_id, _child)
