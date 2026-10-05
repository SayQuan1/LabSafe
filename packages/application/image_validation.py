"""Real image preparation. Object I/O and decoding never receive a DB connection."""

import hashlib
import warnings
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from packages.domain.image_validation import (
    CONTENT_ERRORS,
    RejectedImage,
    ValidatedImage,
    image_keys,
)
from packages.domain.security import ServiceError
from packages.storage.s3 import ObjectVersion

FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


def dimensions(width, height):
    if not (1 <= width <= 10000 and 1 <= height <= 10000 and width * height <= 40_000_000):
        raise ServiceError("IMAGE_TOO_LARGE", 422, "Image dimensions exceed limits")


def normalize(payload, mime):
    """Fully decode one static image; transpose once; create metadata-free RGB PNG."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as probe:
                dimensions(*probe.size)
                if FORMATS.get(probe.format) != mime:
                    raise ServiceError("UNSUPPORTED_MEDIA_TYPE", 422, "Image format mismatch")
                if getattr(probe, "n_frames", 1) != 1:
                    raise ServiceError("IMAGE_INVALID", 422, "Animated images are unsupported")
                probe.verify()
            with Image.open(BytesIO(payload)) as source:
                source.load()  # Keep Pillow's default rejection of truncated input.
                with ImageOps.exif_transpose(source) as oriented:
                    with oriented.convert("RGB") as rgb:
                        dimensions(*rgb.size)
                        # frombytes discards every info/EXIF/ICC/text field, including GPS.
                        with Image.frombytes("RGB", rgb.size, rgb.tobytes()) as clean:
                            output = BytesIO()
                            clean.save(output, format="PNG", compress_level=6, optimize=False)
                            return output.getvalue(), clean.width, clean.height
    except ServiceError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ServiceError("IMAGE_TOO_LARGE", 422, "Image dimensions exceed limits") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, EOFError):
        raise ServiceError("IMAGE_INVALID", 422, "Image cannot be decoded") from None


def prepare_image(storage, tenant, input):
    pinned = ObjectVersion(input.key, input.object_version, input.size_bytes, input.mime_type)
    try:
        original = storage.read_staging(tenant, input.upload_id, pinned, input.expected_sha256)
        analysis, width, height = normalize(original, input.mime_type)
        analysis_sha = hashlib.sha256(analysis).hexdigest()
        original_key, analysis_key = image_keys(tenant, input, analysis_sha)
        original_version = storage.preserve_original(pinned, original_key, input.expected_sha256)
        analysis_version = storage.put_analysis(analysis_key, analysis)
        return ValidatedImage(
            original_key,
            original_version,
            analysis_key,
            analysis_version,
            analysis_sha,
            width,
            height,
        )
    except ServiceError as error:
        if error.code in CONTENT_ERRORS:
            return RejectedImage(error.code)
        raise
