"""Rebuildable perspective-rgb-v1 crops without AI or business dependencies."""

import hashlib
import io
import math
from dataclasses import dataclass
from importlib.metadata import version

TRANSFORM_VERSION = "perspective-rgb-v1"
PNG_ENCODING = "pillow-rgb-png-compress9-v1"
MAX_SIDE = 2048


class CropError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CropTransform:
    quad: tuple[tuple[float, float], ...]
    output_width: int
    output_height: int
    transform_version: str = TRANSFORM_VERSION

    def validate(self):
        if self.transform_version != TRANSFORM_VERSION:
            raise CropError("SCHEMA_MISMATCH", "Unknown crop transform")
        if any(
            type(side) is not int or not 1 <= side <= MAX_SIDE
            for side in (self.output_width, self.output_height)
        ):
            raise CropError("SCHEMA_MISMATCH", "Invalid crop output size")
        if not isinstance(self.quad, (tuple, list)) or len(self.quad) != 4:
            raise CropError("SCHEMA_MISMATCH", "Crop needs four points")
        for point in self.quad:
            if not isinstance(point, (tuple, list)) or len(point) != 2:
                raise CropError("SCHEMA_MISMATCH", "Invalid crop point")
            if any(
                type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                for value in point
            ):
                raise CropError("SCHEMA_MISMATCH", "Crop point must be finite and normalized")

    def as_dict(self):
        self.validate()
        return {
            "quad": [{"x": x, "y": y} for x, y in self.quad],
            "output_width": self.output_width,
            "output_height": self.output_height,
            "transform_version": self.transform_version,
        }

    @classmethod
    def from_dict(cls, value):
        keys = {"quad", "output_width", "output_height", "transform_version"}
        if not isinstance(value, dict) or set(value) != keys:
            raise CropError("SCHEMA_MISMATCH", "Invalid crop transform fields")
        points = value["quad"]
        if not isinstance(points, list) or any(
            not isinstance(point, dict) or set(point) != {"x", "y"} for point in points
        ):
            raise CropError("SCHEMA_MISMATCH", "Invalid crop point fields")
        recipe = cls(
            tuple((point["x"], point["y"]) for point in points),
            value["output_width"],
            value["output_height"],
            value["transform_version"],
        )
        recipe.validate()
        return recipe


def _rgb(rgb):
    import numpy as np

    if (
        not isinstance(rgb, np.ndarray)
        or rgb.dtype != np.uint8
        or rgb.ndim != 3
        or rgb.shape[2] != 3
        or min(rgb.shape[:2]) < 1
        or max(rgb.shape[:2]) > 10000
        or rgb.shape[0] * rgb.shape[1] > 40_000_000
    ):
        raise CropError("SCHEMA_MISMATCH", "Expected bounded RGB8 pixels")


def perspective_rgb(rgb, recipe):
    """Use the serialized normalized recipe as the sole source of crop geometry."""
    import cv2
    import numpy as np

    _rgb(rgb)
    if not isinstance(recipe, CropTransform):
        raise CropError("SCHEMA_MISMATCH", "Expected a crop transform")
    recipe.validate()
    height, width = rgb.shape[:2]
    # Multiply in float64, then cast once. Both processes follow this path.
    points = (np.asarray(recipe.quad, dtype=np.float64) * [width - 1, height - 1]).astype(
        np.float32
    )
    edges = np.roll(points, -1, axis=0) - points
    following = np.roll(edges, -1, axis=0)
    cross = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
    if not (cross > 0).all() or cv2.contourArea(points) < 1:
        raise CropError("MODEL_ERROR", "Crop must be convex, clockwise and have pixel area")
    out_w, out_h = recipe.output_width, recipe.output_height
    target = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(points, target)
    if not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-8:
        raise CropError("MODEL_ERROR", "Crop transform is singular")
    try:
        crop = cv2.warpPerspective(
            rgb,
            matrix,
            (out_w, out_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )
    except cv2.error as error:
        raise CropError("MODEL_ERROR", "Crop transform failed") from error
    return np.ascontiguousarray(crop)


def encode_rgb_png(rgb):
    """Fresh RGB image strips metadata; fixed PNG encoding for byte-level checks."""
    from PIL import Image

    _rgb(rgb)
    with io.BytesIO() as stream, Image.fromarray(rgb) as image:
        image.save(stream, format="PNG", compress_level=9, optimize=False)
        return stream.getvalue()


@dataclass(frozen=True)
class CropEvidence:
    pixels: object
    png: bytes
    sha256: str


def rebuild_crop(rgb, recipe, *, expected_sha256=None):
    """Return pixels/bytes; fail closed if the caller's evidence digest differs."""
    pixels = perspective_rgb(rgb, recipe)
    png = encode_rgb_png(pixels)
    digest = hashlib.sha256(png).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise CropError("HASH_MISMATCH", "Rebuilt crop hash differs")
    return CropEvidence(pixels, png, digest)


def recognition_rgb(pixels, rotation):
    """Explicit OCR-only CCW rotation; the evidence PNG keeps source orientation."""
    import numpy as np

    _rgb(pixels)
    if type(rotation) is not int or rotation not in (0, 90):
        raise CropError("SCHEMA_MISMATCH", "Unsupported recognition rotation")
    return np.ascontiguousarray(np.rot90(pixels) if rotation == 90 else pixels)


def rgb_digest(pixels):
    import numpy as np

    _rgb(pixels)
    height, width = pixels.shape[:2]
    header = f"rgb8:{width}:{height}:".encode("ascii")
    digest = hashlib.sha256(header)
    digest.update(memoryview(np.ascontiguousarray(pixels)))
    return digest.hexdigest()


def rebuild_ocr_evidence(rgb, metadata):
    """Verify source, PNG and the exact oriented RGB input used for recognition."""
    required = {
        "recipe",
        "png_sha256",
        "png_size_bytes",
        "source_rgb_sha256",
        "recognition_rotation_ccw",
        "recognition_width",
        "recognition_height",
        "recognition_rgb_sha256",
    }
    if not isinstance(metadata, dict) or not required <= metadata.keys():
        raise CropError("SCHEMA_MISMATCH", "Incomplete OCR crop evidence")
    for key in ("png_sha256", "source_rgb_sha256", "recognition_rgb_sha256"):
        digest = metadata[key]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise CropError("SCHEMA_MISMATCH", "Invalid evidence digest")
    for key in ("png_size_bytes", "recognition_width", "recognition_height"):
        if type(metadata[key]) is not int or metadata[key] < 1:
            raise CropError("SCHEMA_MISMATCH", "Invalid evidence dimensions or size")
    if rgb_digest(rgb) != metadata["source_rgb_sha256"]:
        raise CropError("HASH_MISMATCH", "Source RGB hash differs")
    recipe = CropTransform.from_dict(metadata["recipe"])
    evidence = rebuild_crop(rgb, recipe, expected_sha256=metadata["png_sha256"])
    rotation = metadata["recognition_rotation_ccw"]
    oriented = recognition_rgb(evidence.pixels, rotation)
    expected_rotation = 90 if recipe.output_height / recipe.output_width >= 1.5 else 0
    if rotation != expected_rotation:
        raise CropError("SCHEMA_MISMATCH", "Recognition rotation differs from the crop policy")
    if (
        len(evidence.png) != metadata["png_size_bytes"]
        or oriented.shape[:2] != (metadata["recognition_height"], metadata["recognition_width"])
        or rgb_digest(oriented) != metadata["recognition_rgb_sha256"]
    ):
        raise CropError("HASH_MISMATCH", "Recognition pixels or PNG size differ")
    return evidence


def crop_runtime():
    import cv2
    from PIL import features

    return {
        "transform_version": TRANSFORM_VERSION,
        "png_encoding": PNG_ENCODING,
        "packages": {name: version(name) for name in ("numpy", "Pillow", "opencv-python-headless")},
        "opencv_build_sha256": hashlib.sha256(cv2.getBuildInformation().encode()).hexdigest(),
        "zlib_version": features.version("zlib"),
        "point_scaling": "float64-widthminus1-heightminus1-then-float32",
        "interpolation": "INTER_LINEAR",
        "border": "BORDER_CONSTANT-rgb0",
        "recognition_rotation": "ccw90-if-height-over-width-ge1.5",
        "recognition_digest": "sha256-ascii-rgb8:width:height:-then-c-order-rgb-bytes",
    }
