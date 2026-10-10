"""Decode only already normalized analysis PNGs, shared by AI and evidence Worker."""

import hashlib
import io
import warnings

from packages.image_evidence.perspective import CropError

MAX_ANALYSIS_BYTES = 128 * 1024 * 1024


def decode_analysis(raw, expected_sha256):
    from PIL import Image, UnidentifiedImageError

    if not 1 <= len(raw) <= MAX_ANALYSIS_BYTES:
        raise CropError("IMAGE_TOO_LARGE", "Analysis byte limit exceeded")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise CropError("HASH_MISMATCH", "Analysis content hash differs")
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CropError("IMAGE_INVALID", "Analysis must be PNG")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if (
                    source.format != "PNG"
                    or source.mode != "RGB"
                    or getattr(source, "n_frames", 1) != 1
                ):
                    raise CropError("IMAGE_INVALID", "Analysis must be a static RGB PNG")
                if max(source.size) > 10000 or source.width * source.height > 40_000_000:
                    raise CropError("IMAGE_TOO_LARGE", "Analysis pixel limit exceeded")
                if source.info:
                    raise CropError("IMAGE_INVALID", "Analysis metadata must already be stripped")
                source.load()
                if source.info:
                    raise CropError("IMAGE_INVALID", "Analysis metadata must already be stripped")
                return source.copy()
    except CropError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise CropError("IMAGE_TOO_LARGE", "Analysis pixel limit exceeded") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise CropError("IMAGE_INVALID", "Analysis PNG is invalid") from None
