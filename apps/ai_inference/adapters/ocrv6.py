"""Local PP-OCRv6_small ONNX adapter; CPU only, no Paddle or downloads."""

import hashlib
import math
from pathlib import Path

from apps.ai_inference.adapters.dfine import AdapterError
from packages.image_evidence.perspective import (
    CropError,
    CropTransform,
    rebuild_crop,
    recognition_rgb,
    rgb_digest,
)

ADAPTER_ID = "ppocrv6-small-db-ctc-cpu-v2"
ARTIFACTS = {
    "det": {
        "inference.onnx": "d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e",
        "inference.yml": "193f435274bf9f0b5f71a929bbfbcf148282df7e633b34e7c373e8f44741b516",
    },
    "rec": {
        "inference.onnx": "5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634",
        "inference.yml": "ab078671bb49f06228eadccd34f1bb501e157f7a047095ffb943ba81512c77d1",
    },
}
MAX_REGIONS = 100
MAX_TEXT = 2000
DET_MAX_SIDE = 960
REC_MAX_WIDTH = 3200


def load_artifacts(directory, role):
    import yaml

    blobs = {}
    for name, expected in ARTIFACTS[role].items():
        try:
            with (Path(directory) / name).open("rb") as stream:
                raw = stream.read(32 * 1024 * 1024 + 1)
        except OSError as error:
            raise AdapterError("MODEL_VERSION_UNAVAILABLE", "OCR artifact unavailable") from error
        if hashlib.sha256(raw).hexdigest() != expected:
            raise AdapterError("HASH_MISMATCH", "OCR artifact hash mismatch")
        blobs[name] = raw
    config = yaml.safe_load(blobs["inference.yml"])
    if config["Global"]["model_name"] != f"PP-OCRv6_small_{role}":
        raise AdapterError("SCHEMA_MISMATCH", "OCR model identity mismatch")
    return blobs["inference.onnx"], config


def validate_session(session, role):
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1 or len(outputs) != 1:
        raise AdapterError("SCHEMA_MISMATCH", "OCR must have one input and one output")
    source, result = inputs[0], outputs[0]
    if (
        source.name != "x"
        or result.name != "fetch_name_0"
        or source.type != "tensor(float)"
        or result.type != "tensor(float)"
        or len(source.shape) != 4
        or source.shape[1] != 3
    ):
        raise AdapterError("SCHEMA_MISMATCH", "OCR tensor signature mismatch")
    if role == "det":
        valid = len(result.shape) == 4 and result.shape[1] == 1
    else:
        valid = source.shape[2] == 48 and len(result.shape) == 3 and result.shape[2] == 18710
    if not valid:
        raise AdapterError("SCHEMA_MISMATCH", "OCR output signature mismatch")
    if session.get_providers() != ["CPUExecutionProvider"]:
        raise AdapterError("MODEL_VERSION_UNAVAILABLE", "OCR requires a CPU-only session")


def _probabilities(array):
    import numpy as np

    if not isinstance(array, np.ndarray) or array.dtype != np.float32:
        raise AdapterError("SCHEMA_MISMATCH", "OCR output must be float32")
    if not np.isfinite(array).all() or np.any(array < 0) or np.any(array > 1):
        raise AdapterError("MODEL_ERROR", "OCR returned invalid probabilities")


def decode_ctc(probs, characters):
    import numpy as np

    _probabilities(probs)
    if probs.ndim != 3 or probs.shape[0] != 1 or probs.shape[1] < 1:
        raise AdapterError("SCHEMA_MISMATCH", "Invalid CTC batch or sequence")
    if probs.shape[2] != len(characters):
        raise AdapterError("SCHEMA_MISMATCH", "CTC vocabulary mismatch")
    if not np.allclose(probs.sum(axis=2), 1, atol=1e-4, rtol=1e-4):
        raise AdapterError("MODEL_ERROR", "CTC probabilities are not normalized")
    indices = probs[0].argmax(axis=1)
    text, scores, previous = [], [], -1
    for step, index in enumerate(indices):
        index = int(index)
        if index != 0 and index != previous:
            text.append(characters[index])
            scores.append(float(probs[0, step, index]))
        previous = index
    decoded = "".join(text)
    if len(decoded) > MAX_TEXT:
        raise AdapterError("MODEL_ERROR", "OCR text exceeds the capacity limit")
    return decoded, sum(scores) / len(scores) if scores else 0.0


def det_tensor(rgb):
    import cv2
    import numpy as np

    height, width = rgb.shape[:2]
    ratio = min(1.0, DET_MAX_SIDE / max(height, width))
    out_h = max(32, int(round(height * ratio / 32)) * 32)
    out_w = max(32, int(round(width * ratio / 32)) * 32)
    bgr = cv2.resize(rgb[:, :, ::-1], (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    pixels = bgr.astype(np.float32) / np.float32(255)
    pixels = (pixels - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array(
        [0.229, 0.224, 0.225], dtype=np.float32
    )
    return np.ascontiguousarray(pixels.transpose(2, 0, 1)[None])


def rec_tensor(rgb):
    import cv2
    import numpy as np

    height, width = rgb.shape[:2]
    target = max(320, math.ceil(48 * width / height / 8) * 8)
    if target > REC_MAX_WIDTH:
        raise AdapterError("MODEL_ERROR", "OCR line exceeds the recognition width limit")
    resized_w = min(target, math.ceil(48 * width / height))
    bgr = cv2.resize(rgb[:, :, ::-1], (resized_w, 48), interpolation=cv2.INTER_LINEAR)
    pixels = bgr.astype(np.float32).transpose(2, 0, 1) / np.float32(127.5) - np.float32(1)
    padded = np.zeros((1, 3, 48, target), dtype=np.float32)
    padded[0, :, :, :resized_w] = pixels
    return padded


def _quad(points):
    import numpy as np

    points = np.asarray(points, dtype=np.float32).reshape(4, 2)
    left_right = points[np.argsort(points[:, 0], kind="stable")]
    left = left_right[:2][np.argsort(left_right[:2, 1], kind="stable")]
    right = left_right[2:][np.argsort(left_right[2:, 1], kind="stable")]
    return np.array([left[0], right[0], right[1], left[1]], dtype=np.float32)


def db_regions(probs, original_size):
    import cv2
    import numpy as np
    import pyclipper

    _probabilities(probs)
    if probs.ndim != 4 or probs.shape[:2] != (1, 1) or min(probs.shape[2:]) < 1:
        raise AdapterError("SCHEMA_MISMATCH", "Invalid DB probability map")
    width, height = original_size
    bitmap = probs[0, 0]
    contours, _ = cv2.findContours(
        (bitmap > 0.2).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )
    if len(contours) > 3000:
        raise AdapterError("MODEL_ERROR", "OCR contour capacity exceeded")
    result = []
    for contour in contours:
        rect = cv2.minAreaRect(contour)
        if min(rect[1]) < 3:
            continue
        points = _quad(cv2.boxPoints(rect))
        x0, y0 = np.floor(points.min(axis=0)).astype(int)
        x1, y1 = np.ceil(points.max(axis=0)).astype(int)
        x0, x1 = max(0, x0), min(bitmap.shape[1] - 1, x1)
        y0, y1 = max(0, y0), min(bitmap.shape[0] - 1, y1)
        mask = np.zeros((y1 - y0 + 1, x1 - x0 + 1), dtype=np.uint8)
        cv2.fillPoly(mask, [np.round(points - [x0, y0]).astype(np.int32)], 1)
        score = float(cv2.mean(bitmap[y0 : y1 + 1, x0 : x1 + 1], mask)[0])
        if score < 0.45:
            continue
        perimeter = cv2.arcLength(points, True)
        distance = cv2.contourArea(points) * 1.4 / perimeter
        offset = pyclipper.PyclipperOffset()
        offset.AddPath(
            np.round(points * 1024).astype(np.int64).tolist(),
            pyclipper.JT_ROUND,
            pyclipper.ET_CLOSEDPOLYGON,
        )
        expanded = offset.Execute(distance * 1024)
        if len(expanded) != 1:
            continue
        rect = cv2.minAreaRect(np.asarray(expanded[0], dtype=np.float32) / 1024)
        if min(rect[1]) < 5:
            continue
        points = _quad(cv2.boxPoints(rect))
        points[:, 0] = np.clip(np.round(points[:, 0] / bitmap.shape[1] * width), 0, width - 1)
        points[:, 1] = np.clip(np.round(points[:, 1] / bitmap.shape[0] * height), 0, height - 1)
        if cv2.contourArea(points) < 1:
            continue
        result.append({"quad_pixels": points, "detection_confidence": score})
    if len(result) > MAX_REGIONS:
        raise AdapterError("MODEL_ERROR", "OCR region capacity exceeded")
    result.sort(
        key=lambda r: tuple(r["quad_pixels"].min(axis=0)[::-1]) + tuple(r["quad_pixels"].flatten())
    )
    return result


def crop_with_evidence(rgb, points):
    import cv2
    import numpy as np

    points = np.asarray(points, dtype=np.float32)
    if (
        points.shape != (4, 2)
        or not np.isfinite(points).all()
        or not cv2.isContourConvex(points)
        or cv2.contourArea(points) < 1
    ):
        raise AdapterError("MODEL_ERROR", "OCR crop quadrilateral is invalid")
    tl, tr, br, bl = points
    width = int(max(np.linalg.norm(tl - tr), np.linalg.norm(bl - br)))
    height = int(max(np.linalg.norm(tl - bl), np.linalg.norm(tr - br)))
    if min(width, height) < 2 or max(width, height) > 2048:
        raise AdapterError("MODEL_ERROR", "OCR crop dimensions are invalid")
    source_h, source_w = rgb.shape[:2]
    normalized = points.astype(np.float64) / [max(1, source_w - 1), max(1, source_h - 1)]
    recipe = CropTransform(tuple(map(tuple, normalized.tolist())), width, height)
    rotation = 90 if height / width >= 1.5 else 0
    try:
        # Recognition is derived from the serialized recipe, never a separate warp.
        evidence = rebuild_crop(rgb, recipe)
        crop = recognition_rgb(evidence.pixels, rotation)
    except CropError as error:
        raise AdapterError(error.code, "OCR evidence crop failed") from error
    return crop, {
        "recipe": recipe.as_dict(),
        "png_sha256": evidence.sha256,
        "png_size_bytes": len(evidence.png),
        "recognition_rotation_ccw": rotation,
        "recognition_width": crop.shape[1],
        "recognition_height": crop.shape[0],
        "recognition_rgb_sha256": rgb_digest(crop),
    }


def crop_region(rgb, points):
    return crop_with_evidence(rgb, points)[0]


class OnnxOCR:
    def __init__(self, det, rec, characters):
        validate_session(det, "det")
        validate_session(rec, "rec")
        if len(characters) != 18710 or characters[0] != "" or characters[-1] != " ":
            raise AdapterError("SCHEMA_MISMATCH", "Unexpected CTC dictionary")
        self.det, self.rec, self.characters = det, rec, characters

    @classmethod
    def from_directories(cls, det_dir, rec_dir, threads=2):
        import onnxruntime as ort

        if type(threads) is not int or not 1 <= threads <= 16:
            raise AdapterError("VALIDATION_ERROR", "Invalid OCR CPU threads")
        det_bytes, det_config = load_artifacts(det_dir, "det")
        rec_bytes, rec_config = load_artifacts(rec_dir, "rec")
        if det_config["PostProcess"] != {
            "name": "DBPostProcess",
            "thresh": 0.2,
            "box_thresh": 0.45,
            "max_candidates": 3000,
            "unclip_ratio": 1.4,
        }:
            raise AdapterError("SCHEMA_MISMATCH", "Unsupported DB parameters")
        chars = ("", *rec_config["PostProcess"]["character_dict"], " ")
        if len(set(chars)) != len(chars):
            raise AdapterError("SCHEMA_MISMATCH", "Duplicate CTC dictionary entries")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            det = ort.InferenceSession(
                det_bytes, sess_options=options, providers=["CPUExecutionProvider"]
            )
            rec = ort.InferenceSession(
                rec_bytes, sess_options=options, providers=["CPUExecutionProvider"]
            )
            det.disable_fallback()
            rec.disable_fallback()
        except Exception as error:
            raise AdapterError("MODEL_VERSION_UNAVAILABLE", "OCR model loading failed") from error
        return cls(det, rec, chars)

    @staticmethod
    def _run(session, tensor):
        try:
            outputs = session.run(["fetch_name_0"], {"x": tensor})
        except Exception as error:
            raise AdapterError("MODEL_ERROR", "OCR inference failed") from error
        if not isinstance(outputs, (list, tuple)) or len(outputs) != 1:
            raise AdapterError("SCHEMA_MISMATCH", "Unexpected OCR output count")
        return outputs[0]

    def recognize(self, image):
        import numpy as np
        from PIL import Image

        if not isinstance(image, Image.Image) or image.mode != "RGB":
            raise AdapterError("SCHEMA_MISMATCH", "OCR input must be RGB")
        rgb = np.asarray(image, dtype=np.uint8)
        tensor = det_tensor(rgb)
        probs = self._run(self.det, tensor)
        if getattr(probs, "shape", None) != (1, 1, *tensor.shape[2:]):
            raise AdapterError("SCHEMA_MISMATCH", "DB output spatial shape mismatch")
        rows = db_regions(probs, image.size)
        source_digest = rgb_digest(rgb)
        for row in rows:
            crop, row["crop_evidence"] = crop_with_evidence(rgb, row["quad_pixels"])
            row["crop_evidence"]["source_rgb_sha256"] = source_digest
            rec_input = rec_tensor(crop)
            probs = self._run(self.rec, rec_input)
            if getattr(probs, "shape", ()) != (1, rec_input.shape[3] // 8, 18710):
                raise AdapterError("SCHEMA_MISMATCH", "CTC output sequence shape mismatch")
            row["text"], row["confidence"] = decode_ctc(probs, self.characters)
            row["quad"] = (
                row.pop("quad_pixels")
                / np.array([max(1, image.width - 1), max(1, image.height - 1)])
            ).tolist()
        return rows

    def smoke(self):
        import numpy as np
        from PIL import Image

        with Image.new("RGB", (96, 64), "white") as image:
            self.recognize(image)
        probs = self._run(self.rec, np.zeros((1, 3, 48, 320), dtype=np.float32))
        decode_ctc(probs, self.characters)
