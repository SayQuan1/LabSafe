"""Immutable Worker artifacts and canonical derivative namespace."""

from dataclasses import dataclass

from packages.domain.inference_execution import InferenceInvalidResult
from packages.domain.uploads import digest, identifier


def derivative_key(tenant, laboratory, run, crop, sha256):
    for value in (tenant, laboratory, run, crop):
        identifier(value)
    digest(sha256)
    return f"tenant/{tenant}/lab/{laboratory}/derivatives/{run}/{crop}/{sha256}.png"


@dataclass(frozen=True)
class InferenceDerivative:
    image_id: str
    crop_id: str
    line_id: str
    detection_id: str | None
    object_key: str
    object_version: str
    sha256: str
    size_bytes: int


def validate_derivatives(lease, result, derivatives):
    if not isinstance(derivatives, tuple) or len(derivatives) != len(result["crops"]):
        raise InferenceInvalidResult()
    artifacts = {row.crop_id: row for row in derivatives if isinstance(row, InferenceDerivative)}
    if len(artifacts) != len(derivatives):
        raise InferenceInvalidResult()
    for crop in result["crops"]:
        artifact = artifacts.get(crop["crop_id"])
        if artifact is None or any(
            getattr(artifact, key) != crop[key]
            for key in ("image_id", "crop_id", "line_id", "detection_id")
        ):
            raise InferenceInvalidResult()
        if (
            artifact.sha256 != crop["evidence"]["png_sha256"]
            or type(artifact.size_bytes) is not int
            or artifact.size_bytes != crop["evidence"]["png_size_bytes"]
            or artifact.object_key
            != derivative_key(
                lease.tenant_id,
                lease.input.laboratory_id,
                lease.input.run_id,
                crop["crop_id"],
                artifact.sha256,
            )
            or not isinstance(artifact.object_version, str)
            or not 1 <= len(artifact.object_version) <= 200
            or artifact.object_version == "null"
            or any(not 33 <= ord(c) <= 126 for c in artifact.object_version)
        ):
            raise InferenceInvalidResult()
