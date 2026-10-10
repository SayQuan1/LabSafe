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
from packages.inference_protocol.association import ALGORITHM_ID, associate_rows
from packages.inference_protocol.chemical import ALGORITHM_ID as CHEMICAL_ID
from packages.inference_protocol.chemical import extract_entities
from packages.inference_protocol.fields import ALGORITHM_ID as FIELDS_ID
from packages.inference_protocol.fields import extract_fields

PIPELINE_ID = "quality-coco80-ocrv6-cpu-v5"


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
    entity_min: float = 0.9

    def validate(self):
        CpuOptions("", "", "", self.detection_min, self.threads, self.timeout_seconds).validate()
        OcrOptions("", "", self.text_min, self.threads, self.timeout_seconds).validate()
        if type(self.quality_only) is not bool or not isinstance(self.quality, QualityThresholds):
            raise AdapterError("VALIDATION_ERROR", "Invalid pipeline configuration")
        self.quality.validate()
        if type(self.entity_min) not in (int, float) or not 0 <= self.entity_min <= 1:
            raise AdapterError("VALIDATION_ERROR", "Invalid entity threshold")
        if not self.quality_only and any(
            not isinstance(p, str) or not p for p in (self.model_path, self.det_dir, self.rec_dir)
        ):
            raise AdapterError("VALIDATION_ERROR", "Detector and OCR paths are required")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class LoadedModels:
    detector: object
    ocr: object
    artifacts: dict
    runtime: dict
    loaded_ms: int


def load_models(options):
    from PIL import Image

    runtime = {}
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
    return LoadedModels(detector, ocr, artifacts, runtime, loaded_ms)


def _compute(options, paths, run_id, *, decoder=None, image_ids=None, models=None, dictionary=None):
    """Decode once, gate the whole batch, then load each session once."""
    started = time.monotonic()
    decoded, images = [], []
    quality_ms = association_ms = 0
    try:
        for ordinal, path in enumerate(paths):
            image, digest = (decoder or _decode_local)(path)
            decoded.append(image)
            checked_at = time.monotonic()
            quality = evaluate_quality(image, options.quality)
            quality_ms += round((time.monotonic() - checked_at) * 1000)
            image_id = (
                image_ids[ordinal]
                if image_ids is not None
                else str(uuid5(UUID(run_id), f"image:{ordinal}:{digest}"))
            )
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
            loaded = models if models is not None else load_models(options)
            detector, ocr = loaded.detector, loaded.ocr
            artifacts = loaded.artifacts
            runtime.update(loaded.runtime)
            loaded_ms = 0 if models is not None else loaded.loaded_ms
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
                associated_at = time.monotonic()
                try:
                    associate_rows(lines, row["detections"])
                except ValueError:
                    raise AdapterError("MODEL_ERROR", "Invalid text association geometry") from None
                association_ms += round((time.monotonic() - associated_at) * 1000)
                row.update(inference_executed=True, lines=lines, detection_diagnostics=diagnostics)
        from packages.inference_protocol.evidence import text_evidence

        text_regions, crops = text_evidence([line for row in images for line in row["lines"]])
        fields_at = time.monotonic()
        try:
            ocr_fields = extract_fields(text_regions, dictionary)
            entities = (
                extract_entities(ocr_fields, dictionary, options.text_min, options.entity_min)
                if dictionary is not None
                else []
            )
        except ValueError:
            raise AdapterError("MODEL_ERROR", "Invalid OCR fields or capacity") from None
        fields_ms = round((time.monotonic() - fields_at) * 1000)
        config = {
            "pipeline_id": PIPELINE_ID,
            "detection_min": options.detection_min,
            "text_min": options.text_min,
            "quality": asdict(options.quality),
            "quality_only": options.quality_only,
            "text_association": ALGORITHM_ID,
            "ocr_fields": FIELDS_ID,
            "chemical_candidates": CHEMICAL_ID,
            "entity_min": options.entity_min,
        }
        return {
            "schema_version": "cpu-pipeline-local-v5",
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
                "chemical_candidates": models_executed and dictionary is not None,
                "container_relations": False,
                "bottle_association": models_executed,
                "ocr_fields": models_executed,
                "date_facts": False,
            },
            "timing_ms": {
                "quality_ms": quality_ms,
                "association_ms": association_ms,
                "fields_ms": fields_ms,
                "load_and_smoke_ms": loaded_ms,
                "total_ms": round((time.monotonic() - started) * 1000),
            },
            "images": images,
            "text_regions": text_regions,
            "crops": crops,
            "ocr_fields": ocr_fields,
            "entities": entities,
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
