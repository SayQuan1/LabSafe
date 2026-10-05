"""Trusted validator outcomes and deterministic object references."""

from dataclasses import dataclass

from packages.domain.uploads import digest, identifier

CONTENT_ERRORS = frozenset(
    {
        "OBJECT_NOT_FOUND",
        "IMAGE_INVALID",
        "IMAGE_TOO_LARGE",
        "UNSUPPORTED_MEDIA_TYPE",
        "HASH_MISMATCH",
    }
)
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def image_keys(tenant, input, analysis_sha):
    for value in (tenant, input.laboratory_id, input.image_id):
        identifier(value)
    digest(input.expected_sha256)
    digest(analysis_sha)
    root = f"tenant/{tenant}/lab/{input.laboratory_id}"
    return (
        f"{root}/original/{input.image_id}/{input.expected_sha256}.{EXTENSIONS[input.mime_type]}",
        f"{root}/analysis/{input.image_id}/{analysis_sha}.png",
    )


@dataclass(frozen=True)
class ValidatedImage:
    original_key: str
    original_version: str
    analysis_key: str
    analysis_version: str
    analysis_sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class RejectedImage:
    code: str

    def __post_init__(self):
        if self.code not in CONTENT_ERRORS:
            raise ValueError("Not an image content error")


def validate_result(tenant, input, result):
    if not isinstance(result, ValidatedImage):
        raise ValueError("Invalid ready image outcome")
    digest(result.analysis_sha256)
    if image_keys(tenant, input, result.analysis_sha256) != (
        result.original_key,
        result.analysis_key,
    ):
        raise ValueError("Invalid image result object keys")
    for version in (result.original_version, result.analysis_version):
        if (
            not isinstance(version, str)
            or not 1 <= len(version) <= 200
            or version == "null"
            or any(not 33 <= ord(c) <= 126 for c in version)
        ):
            raise ValueError("Invalid image result object version")
    if (
        type(result.width) is not int
        or type(result.height) is not int
        or not 1 <= result.width <= 10000
        or not 1 <= result.height <= 10000
        or result.width * result.height > 40_000_000
    ):
        raise ValueError("Invalid image result dimensions")
