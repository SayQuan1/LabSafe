"""Real RGB quality-rgb-lap1-v1 pixel implementation, independent of models."""

import math
from dataclasses import asdict, dataclass
from importlib.metadata import version

from apps.ai_inference.adapters.dfine import AdapterError

ALGORITHM_ID = "quality-rgb-lap1-v1"


@dataclass(frozen=True)
class QualityThresholds:
    blur_min: float = 80.0
    dark_min: float = 0.12
    glare_max: float = 0.30

    def validate(self):
        for value in (self.blur_min, self.dark_min, self.glare_max):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise AdapterError("VALIDATION_ERROR", "Quality thresholds must be finite numbers")
        if self.blur_min < 0 or not 0 <= self.dark_min <= 1 or not 0 <= self.glare_max <= 1:
            raise AdapterError("VALIDATION_ERROR", "Quality thresholds are outside their ranges")


def resize_shape(width, height):
    if type(width) is not int or type(height) is not int or min(width, height) < 1:
        raise AdapterError("IMAGE_INVALID", "Invalid quality image dimensions")
    longest = max(width, height)
    if longest <= 1024:
        return width, height
    return tuple(max(1, (v * 1024 + longest // 2) // longest) for v in (width, height))


def quality_runtime():
    return {
        "algorithm_id": ALGORITHM_ID,
        "packages": {name: version(name) for name in ("numpy", "Pillow")},
        "resize": {
            "max_side": 1024,
            "interpolation": "Pillow-BILINEAR",
            "rounding": "integer-half-up",
            "upscale": False,
            "padding": False,
        },
        "gray": {"weights": [77, 150, 29], "offset": 128, "divisor": 256, "dtype": "uint32"},
        "laplacian": {
            "kernel": [[0, 1, 0], [1, -4, 1], [0, 1, 0]],
            "border": "REFLECT_101",
            "singleton_axis": "repeat",
            "dtype": "float64",
            "variance_ddof": 0,
        },
        "brightness_divisor": 255,
        "glare_gray_min": 250,
        "round_scores": False,
    }


def evaluate_quality(image, thresholds=QualityThresholds()):
    """RGB input is already oriented. Never rotate, enlarge, or convert to BGR."""
    import numpy as np
    from PIL import Image

    if not isinstance(image, Image.Image) or image.mode != "RGB":
        raise AdapterError("SCHEMA_MISMATCH", "Quality input must be RGB")
    if not isinstance(thresholds, QualityThresholds):
        raise AdapterError("VALIDATION_ERROR", "Invalid quality configuration")
    thresholds.validate()
    size = resize_shape(*image.size)
    resized = image.resize(size, Image.Resampling.BILINEAR) if size != image.size else image
    try:
        pixels = np.asarray(resized, dtype=np.uint32)
        gray = (77 * pixels[:, :, 0] + 150 * pixels[:, :, 1] + 29 * pixels[:, :, 2] + 128) // 256
        gray = gray.astype(np.uint8)
    finally:
        if resized is not image:
            resized.close()
    plane = gray.astype(np.float64)
    # numpy reflect repeats the sole value when an axis has length one.
    padded = np.pad(plane, 1, mode="reflect")
    lap = padded[:-2, 1:-1] + padded[2:, 1:-1] + padded[1:-1, :-2] + padded[1:-1, 2:] - 4 * plane
    scores = {
        "blur_score": float(lap.var(dtype=np.float64, ddof=0)),
        "brightness": float(plane.mean(dtype=np.float64) / 255),
        "glare_ratio": float(np.count_nonzero(gray >= 250) / gray.size),
    }
    reasons = [
        name
        for name, failed in (
            ("blur", scores["blur_score"] < thresholds.blur_min),
            ("dark", scores["brightness"] < thresholds.dark_min),
            ("glare", scores["glare_ratio"] > thresholds.glare_max),
        )
        if failed
    ]
    return {
        "algorithm_id": ALGORITHM_ID,
        "analysis_size": list(size),
        **scores,
        "passed": not reasons,
        "reasons": reasons,
        "thresholds": asdict(thresholds),
    }
