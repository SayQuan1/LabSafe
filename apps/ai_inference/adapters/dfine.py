"""Fail-closed adapter for the official D-FINE COCO-80 ONNX export.

The adapter is deliberately independent from the business application.  It only
loads a previously verified artifact, validates the ONNX signature and turns the
300 raw or post-processed queries into bounded detections.  COCO labels remain
the model labels; no project-specific four-class remapping is performed.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ADAPTER_ID = "dfine-coco80-rgb-stretch-v1"
MODEL_FAMILY = "dfine_n"
QUERY_COUNT = 300
CLASS_COUNT = 80
INPUT_SIZE = (640, 640)
# This ordering is the id2label ordering in the supplied official config.json.
CLASSES = (
    "person",
    "bicycle",
    "car",
    "motorbike",
    "aeroplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "sofa",
    "pottedplant",
    "bed",
    "diningtable",
    "toilet",
    "tvmonitor",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)
MAX_DETECTIONS = 100
OFFICIAL_MODEL_SHA256 = "0f684f409618ee8a822410e754a29caa817d1aa16283ce89cad936d0a48e2f35"
OFFICIAL_CONFIG_SHA256 = "a5c7533f3b72be6bb102b93e1b34ca3643af4e0590408a7881543cbb0aa80c4c"
OFFICIAL_PREPROCESSOR_SHA256 = "cd38cd59999e7a95d68e487fbe5132df3d4e5c32a0836add57e6126ba0c4eaf1"


class AdapterError(ValueError):
    """Stable adapter validation failure with a public error category."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ArtifactEvidence:
    model_sha256: str
    config_sha256: str
    preprocessor_sha256: str
    model_bytes: int
    config: dict[str, Any]
    preprocessor: dict[str, Any]
    development_only: bool
    activation_allowed: bool


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except (OSError, TypeError) as error:
        raise AdapterError("MODEL_VERSION_UNAVAILABLE", "Model artifact cannot be read") from error
    if size < 1:
        raise AdapterError("MODEL_VERSION_UNAVAILABLE", "Model artifact is empty")
    return digest.hexdigest(), size


def _json(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise AdapterError("SCHEMA_MISMATCH", "Model metadata is not valid JSON") from error
    if not isinstance(value, dict):
        raise AdapterError("SCHEMA_MISMATCH", "Model metadata must be an object")
    return hashlib.sha256(raw).hexdigest(), value


def inspect_artifacts(
    model_path: str | Path,
    config_path: str | Path,
    preprocessor_path: str | Path,
    *,
    enforce_known_hash: bool = False,
):
    """Hash and inspect supplied official artifacts without loading executable code."""
    model = Path(model_path)
    config_digest, config = _json(Path(config_path))
    preprocessor_digest, preprocessor = _json(Path(preprocessor_path))
    model_digest, model_bytes = _sha256(model)
    if enforce_known_hash and (
        model_digest != OFFICIAL_MODEL_SHA256
        or config_digest != OFFICIAL_CONFIG_SHA256
        or preprocessor_digest != OFFICIAL_PREPROCESSOR_SHA256
    ):
        raise AdapterError(
            "MODEL_VERSION_UNAVAILABLE",
            "Model artifact hash is not the approved official version",
        )
    labels = config.get("id2label")
    expected_labels = {str(index): label for index, label in enumerate(CLASSES)}
    official_coco = (
        config.get("model_type") == "d_fine"
        and config.get("num_queries") == QUERY_COUNT
        and labels == expected_labels
        and preprocessor.get("size") == {"height": 640, "width": 640}
        and preprocessor.get("do_resize") is True
        and preprocessor.get("do_rescale") is True
        and preprocessor.get("do_pad") is False
        and preprocessor.get("do_normalize") is False
        and preprocessor.get("rescale_factor") == 1 / 255
    )
    if not official_coco:
        raise AdapterError("SCHEMA_MISMATCH", "Unsupported D-FINE artifact metadata")
    return ArtifactEvidence(
        model_digest,
        config_digest,
        preprocessor_digest,
        model_bytes,
        config,
        preprocessor,
        # The official checkpoint is usable for development/evaluation immediately.
        # Production still requires the normal evaluation and release approval gates.
        development_only=True,
        activation_allowed=True,
    )


def require_activation(evidence: ArtifactEvidence) -> None:
    if not isinstance(evidence, ArtifactEvidence) or not evidence.activation_allowed:
        raise AdapterError(
            "MODEL_VERSION_UNAVAILABLE",
            "The supplied D-FINE artifact is not approved for activation",
        )


def _labels_from_config(evidence: ArtifactEvidence | None) -> tuple[str, ...]:
    if evidence is None:
        return CLASSES
    labels = evidence.config.get("id2label")
    if not isinstance(labels, dict):
        raise AdapterError("SCHEMA_MISMATCH", "Model config has no id2label map")
    try:
        resolved = tuple(labels[str(index)] for index in range(CLASS_COUNT))
    except (KeyError, TypeError) as error:
        raise AdapterError("SCHEMA_MISMATCH", "Model config label map is incomplete") from error
    if resolved != CLASSES:
        raise AdapterError("SCHEMA_MISMATCH", "Model config label map does not match COCO-80")
    return resolved


def preprocess_rgb(image):
    """Return direct RGB stretch tensors and ONNX target size ``[H,W]``."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError as error:
        raise AdapterError(
            "MODEL_VERSION_UNAVAILABLE", "AI runtime dependencies are unavailable"
        ) from error
    if not isinstance(image, Image.Image) or image.mode != "RGB":
        raise AdapterError("SCHEMA_MISMATCH", "Detector input must be an RGB image")
    width, height = image.size
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise AdapterError("SCHEMA_MISMATCH", "Detector image dimensions are invalid")
    resized = image.resize(INPUT_SIZE, Image.Resampling.BILINEAR)
    array = np.asarray(resized, dtype=np.uint8).astype(np.float32) / np.float32(255.0)
    tensor = np.ascontiguousarray(np.transpose(array, (2, 0, 1))[None, ...], dtype=np.float32)
    # RT-DETR/D-FINE post-processing follows the torchvision convention [height,width].
    target_sizes = np.asarray([[height, width]], dtype=np.int64)
    return tensor, target_sizes


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _rows(value, name):
    try:
        rows = value.tolist()
    except AttributeError:
        rows = value
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list):
        raise AdapterError("SCHEMA_MISMATCH", f"{name} must have batch dimension 1")
    return rows[0]


def postprocess_project(logits, boxes_cxcywh, original_size, detection_min, *, diagnostics=None):
    """Apply qmax/sigmoid/no-NMS semantics to raw COCO-80 query tensors."""
    try:
        width, height = original_size
    except (TypeError, ValueError):
        raise AdapterError("SCHEMA_MISMATCH", "Image size must be [width,height]") from None
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise AdapterError("SCHEMA_MISMATCH", "Image size must be positive integers")
    if not _finite(detection_min) or not 0 <= detection_min <= 1:
        raise AdapterError("VALIDATION_ERROR", "Invalid detection threshold")
    logit_rows, box_rows = _rows(logits, "logits"), _rows(boxes_cxcywh, "boxes")
    if len(logit_rows) != QUERY_COUNT or len(box_rows) != QUERY_COUNT:
        raise AdapterError("SCHEMA_MISMATCH", "Expected exactly 300 detector queries")
    result = []
    counts = {"candidates": QUERY_COUNT, "below_threshold": 0, "outside_image": 0}
    for query_index, (logit, box) in enumerate(zip(logit_rows, box_rows)):
        if not isinstance(logit, list) or not isinstance(box, list):
            raise AdapterError("SCHEMA_MISMATCH", "Detector rows must be arrays")
        if len(logit) != CLASS_COUNT or len(box) != 4:
            raise AdapterError("SCHEMA_MISMATCH", "Expected 80 classes and four box coordinates")
        if not all(_finite(v) for v in (*logit, *box)):
            raise AdapterError("MODEL_ERROR", "Detector output contains non-finite values")
        label = min(range(CLASS_COUNT), key=lambda index: (-logit[index], index))
        score = (
            1 / (1 + math.exp(-logit[label]))
            if logit[label] >= 0
            else math.exp(logit[label]) / (1 + math.exp(logit[label]))
        )
        # Official raw exports may emit finite negative widths at very low scores.
        # Check finite values for every query, then geometry only for retained queries.
        if score < detection_min:
            counts["below_threshold"] += 1
            continue
        cx, cy, box_width, box_height = box
        if box_width <= 0 or box_height <= 0:
            raise AdapterError("MODEL_ERROR", "Detector output contains a non-positive box")
        xyxy = [
            (cx - box_width / 2) * width,
            (cy - box_height / 2) * height,
            (cx + box_width / 2) * width,
            (cy + box_height / 2) * height,
        ]
        x1, y1, x2, y2 = xyxy
        clipped = [
            max(0, min(width, x1)) / width,
            max(0, min(height, y1)) / height,
            max(0, min(width, x2)) / width,
            max(0, min(height, y2)) / height,
        ]
        if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
            counts["outside_image"] += 1
            continue
        result.append(
            {
                "class_id": label,
                "type": CLASSES[label],
                "bbox": clipped,
                "confidence": score,
                "query_index": query_index,
            }
        )
    result.sort(
        key=lambda row: (
            row["bbox"][1],
            row["bbox"][0],
            row["bbox"][3],
            row["bbox"][2],
            row["class_id"],
            row["confidence"],
            row["query_index"],
        )
    )
    if len(result) > MAX_DETECTIONS:
        raise AdapterError("MODEL_ERROR", "Detector result exceeds the 100-detection limit")
    if diagnostics is not None:
        diagnostics.update(counts, kept=len(result))
    return result


def _metadata_shape(value, name):
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "type", None)
    if shape is None or dtype is None:
        raise AdapterError("SCHEMA_MISMATCH", f"Missing ONNX metadata for {name}")
    return tuple(shape), dtype


def validate_session_contract(session) -> str:
    """Validate names/shapes and return ``raw`` or ``postprocessed`` mode."""
    try:
        inputs = {item.name: item for item in session.get_inputs()}
        outputs = {item.name: item for item in session.get_outputs()}
    except (AttributeError, TypeError) as error:
        raise AdapterError("SCHEMA_MISMATCH", "Invalid ONNX session metadata") from error
    if set(inputs) == {"pixel_values"} and set(outputs) == {"logits", "pred_boxes"}:
        image_shape, image_type = _metadata_shape(inputs["pixel_values"], "pixel_values")
        logits_shape, logits_type = _metadata_shape(outputs["logits"], "logits")
        boxes_shape, boxes_type = _metadata_shape(outputs["pred_boxes"], "pred_boxes")
        if (
            image_shape not in ((1, 3, 640, 640), ("batch_size", 3, "height", "width"))
            or image_type != "tensor(float)"
        ):
            raise AdapterError("SCHEMA_MISMATCH", "Unexpected pixel_values input signature")
        if (
            logits_shape != (image_shape[0], QUERY_COUNT, CLASS_COUNT)
            or logits_type != "tensor(float)"
        ):
            raise AdapterError("SCHEMA_MISMATCH", "Unexpected logits output signature")
        if boxes_shape != (image_shape[0], QUERY_COUNT, 4) or boxes_type != "tensor(float)":
            raise AdapterError("SCHEMA_MISMATCH", "Unexpected pred_boxes output signature")
        return "raw"
    if set(inputs) != {"images", "orig_target_sizes"}:
        raise AdapterError("SCHEMA_MISMATCH", "Unexpected ONNX input names")
    image_shape, image_type = _metadata_shape(inputs["images"], "images")
    size_shape, size_type = _metadata_shape(inputs["orig_target_sizes"], "orig_target_sizes")
    if image_shape != (1, 3, 640, 640) or image_type != "tensor(float)":
        raise AdapterError("SCHEMA_MISMATCH", "Unexpected images input signature")
    if size_shape != (1, 2) or size_type != "tensor(int64)":
        raise AdapterError("SCHEMA_MISMATCH", "Unexpected orig_target_sizes input signature")
    if set(outputs) != {"labels", "boxes", "scores"}:
        raise AdapterError("SCHEMA_MISMATCH", "Unexpected ONNX output names")
    labels_shape, labels_type = _metadata_shape(outputs["labels"], "labels")
    boxes_shape, boxes_type = _metadata_shape(outputs["boxes"], "boxes")
    scores_shape, scores_type = _metadata_shape(outputs["scores"], "scores")
    if labels_shape != (1, 300) or labels_type != "tensor(int64)":
        raise AdapterError("SCHEMA_MISMATCH", "Unexpected labels output signature")
    if boxes_shape != (1, 300, 4) or boxes_type != "tensor(float)":
        raise AdapterError("SCHEMA_MISMATCH", "Unexpected boxes output signature")
    if scores_shape != (1, 300) or scores_type != "tensor(float)":
        raise AdapterError("SCHEMA_MISMATCH", "Unexpected scores output signature")
    return "postprocessed"


def adapt_outputs(labels, boxes_xyxy, scores, original_size, detection_min, *, class_names=None):
    """Validate exported labels/boxes/scores before filtering or clipping."""
    try:
        width, height = original_size
    except (TypeError, ValueError):
        raise AdapterError("SCHEMA_MISMATCH", "Image size must be [width,height]") from None
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise AdapterError("SCHEMA_MISMATCH", "Image size must be positive integers")
    if not _finite(detection_min) or not 0 <= detection_min <= 1:
        raise AdapterError("VALIDATION_ERROR", "Invalid detection threshold")
    label_rows, box_rows, score_rows = (
        _rows(labels, "labels"),
        _rows(boxes_xyxy, "boxes"),
        _rows(scores, "scores"),
    )
    if not len(label_rows) == len(box_rows) == len(score_rows) == QUERY_COUNT:
        raise AdapterError("SCHEMA_MISMATCH", "Expected exactly 300 detector queries")
    names = tuple(class_names) if class_names is not None else CLASSES
    if len(names) != CLASS_COUNT or any(not isinstance(name, str) or not name for name in names):
        raise AdapterError("SCHEMA_MISMATCH", "Detector class map is invalid")
    result = []
    for query_index, (label, box, score) in enumerate(zip(label_rows, box_rows, score_rows)):
        if type(label) is not int or not 0 <= label < CLASS_COUNT:
            raise AdapterError("MODEL_ERROR", "Detector returned an unknown COCO class")
        if not isinstance(box, list) or len(box) != 4:
            raise AdapterError("SCHEMA_MISMATCH", "Detector box shape is invalid")
        if not all(_finite(value) for value in (*box, score)):
            raise AdapterError("MODEL_ERROR", "Detector output contains non-finite values")
        if not 0 <= score <= 1:
            raise AdapterError("MODEL_ERROR", "Detector returned an invalid score")
        if score < detection_min:
            continue
        if box[2] <= box[0] or box[3] <= box[1]:
            raise AdapterError("MODEL_ERROR", "Detector returned an invalid box")
        x1, y1, x2, y2 = box
        clipped = [
            max(0, min(width, x1)) / width,
            max(0, min(height, y1)) / height,
            max(0, min(width, x2)) / width,
            max(0, min(height, y2)) / height,
        ]
        if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
            continue
        result.append(
            {
                "class_id": label,
                "type": names[label],
                "bbox": clipped,
                "confidence": score,
                "query_index": query_index,
            }
        )
    result.sort(
        key=lambda row: (
            row["bbox"][1],
            row["bbox"][0],
            row["bbox"][3],
            row["bbox"][2],
            row["class_id"],
            row["confidence"],
            row["query_index"],
        )
    )
    if len(result) > MAX_DETECTIONS:
        raise AdapterError("MODEL_ERROR", "Detector result exceeds the 100-detection limit")
    return result


class OnnxDetector:
    """Small lazy ONNX Runtime wrapper with an explicit provider selection.

    Importing this module does not import onnxruntime.  This keeps the fixture
    process lightweight while allowing a real worker image to opt into CPU or
    CUDA explicitly via ``providers``.
    """

    def __init__(self, session, *, evidence: ArtifactEvidence | None = None):
        self._mode = validate_session_contract(session)
        self._session = session
        self.evidence = evidence
        self._class_names = _labels_from_config(evidence)

    @classmethod
    def from_path(
        cls,
        model_path: str | Path,
        *,
        providers: list[str] | tuple[str, ...] = ("CPUExecutionProvider",),
        config_path: str | Path | None = None,
        preprocessor_path: str | Path | None = None,
        cpu_threads: int = 2,
    ) -> "OnnxDetector":
        if not providers or any(
            not isinstance(provider, str) or not provider for provider in providers
        ):
            raise AdapterError(
                "VALIDATION_ERROR", "At least one explicit ONNX provider is required"
            )
        if type(cpu_threads) is not int or not 1 <= cpu_threads <= 16:
            raise AdapterError("VALIDATION_ERROR", "CPU threads must be 1..16")
        model_path = Path(model_path)
        evidence = inspect_artifacts(
            model_path,
            config_path or model_path.with_name("config.json"),
            preprocessor_path or model_path.with_name("preprocessor_config.json"),
            enforce_known_hash=True,
        )
        require_activation(evidence)
        try:
            import onnxruntime as ort
        except ImportError as error:
            raise AdapterError(
                "MODEL_VERSION_UNAVAILABLE", "ONNX Runtime is unavailable"
            ) from error
        try:
            available = set(ort.get_available_providers())
            if any(provider not in available for provider in providers):
                raise AdapterError(
                    "MODEL_VERSION_UNAVAILABLE", "Requested ONNX provider is unavailable"
                )
            # Load the exact bytes verified here, avoiding a path replacement between
            # hashing and ONNX Runtime loading the model.
            with model_path.open("rb") as stream:
                model_bytes = stream.read(evidence.model_bytes + 1)
            if hashlib.sha256(model_bytes).hexdigest() != evidence.model_sha256:
                raise AdapterError("HASH_MISMATCH", "Model artifact changed during loading")
            options = ort.SessionOptions()
            options.intra_op_num_threads = cpu_threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            session = ort.InferenceSession(
                model_bytes, sess_options=options, providers=list(providers)
            )
            disable_fallback = getattr(session, "disable_fallback", None)
            if not callable(disable_fallback):
                raise AdapterError(
                    "MODEL_VERSION_UNAVAILABLE", "ONNX fallback control is unavailable"
                )
            disable_fallback()
            if tuple(session.get_providers())[: len(providers)] != tuple(providers):
                raise AdapterError(
                    "MODEL_VERSION_UNAVAILABLE", "ONNX provider order was not honored"
                )
        except AdapterError:
            raise
        except Exception as error:  # runtime errors must not leak filesystem details
            raise AdapterError(
                "MODEL_VERSION_UNAVAILABLE", "ONNX model could not be loaded"
            ) from error
        return cls(session, evidence=evidence)

    @property
    def providers(self) -> tuple[str, ...]:
        try:
            return tuple(self._session.get_providers())
        except AttributeError as error:
            raise AdapterError("SCHEMA_MISMATCH", "Invalid ONNX session") from error

    def detect(
        self, image, detection_min: float = 0.4, *, diagnostics=None
    ) -> list[dict[str, Any]]:
        if not _finite(detection_min) or not 0 <= detection_min <= 1:
            raise AdapterError("VALIDATION_ERROR", "Invalid detection threshold")
        tensor, target_sizes = preprocess_rgb(image)
        width, height = image.size
        try:
            if self._mode == "raw":
                outputs = self._session.run(["logits", "pred_boxes"], {"pixel_values": tensor})
            else:
                outputs = self._session.run(
                    ["labels", "boxes", "scores"],
                    {"images": tensor, "orig_target_sizes": target_sizes},
                )
        except Exception as error:
            raise AdapterError("MODEL_ERROR", "ONNX inference failed") from error
        expected_count = 2 if self._mode == "raw" else 3
        if not isinstance(outputs, (list, tuple)) or len(outputs) != expected_count:
            raise AdapterError("SCHEMA_MISMATCH", "ONNX output count is invalid")
        shapes = (
            ((1, QUERY_COUNT, CLASS_COUNT), (1, QUERY_COUNT, 4))
            if self._mode == "raw"
            else ((1, QUERY_COUNT), (1, QUERY_COUNT, 4), (1, QUERY_COUNT))
        )
        types = ("float32", "float32") if self._mode == "raw" else ("int64", "float32", "float32")
        for output, shape, dtype in zip(outputs, shapes, types):
            if (
                tuple(getattr(output, "shape", ())) != shape
                or str(getattr(output, "dtype", None)) != dtype
            ):
                raise AdapterError("SCHEMA_MISMATCH", "ONNX output tensor signature is invalid")
        if self._mode == "raw":
            return postprocess_project(
                outputs[0], outputs[1], [width, height], detection_min, diagnostics=diagnostics
            )
        return adapt_outputs(
            outputs[0],
            outputs[1],
            outputs[2],
            [width, height],
            detection_min,
            class_names=self._class_names,
        )
