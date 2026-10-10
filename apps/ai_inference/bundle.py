"""Pinned local development bundle; never selects model artifacts from requests."""

import hashlib
import json
import os
import platform
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.adapters.quality import QualityThresholds
from apps.ai_inference.pipeline_cpu import PipelineOptions
from packages.image_evidence.perspective import crop_runtime
from packages.inference_protocol.chemical import dictionary as validate_dictionary
from packages.inference_protocol.contract import validate

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "fastapi",
    "pydantic",
    "uvicorn",
    "PyYAML",
    "jsonschema",
    "rfc3339-validator",
    "onnxruntime",
    "numpy",
    "Pillow",
    "opencv-python-headless",
    "pyclipper",
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def json_digest(value):
    return digest(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    )


def code_digest():
    # Include the service, input reader, algorithms and shared protocol/pixel implementations.
    paths = sorted(
        p
        for directory in (
            ROOT / "apps/ai_inference",
            ROOT / "packages/image_evidence",
            ROOT / "packages/inference_protocol",
        )
        for p in directory.rglob("*.py")
    )
    return json_digest(
        {p.relative_to(ROOT).as_posix(): digest(p.read_bytes()) for p in paths}
        | {
            "packages/inference_protocol/contract.json": digest(
                (ROOT / "packages/inference_protocol/contract.json").read_bytes()
            )
        }
    )


def runtime_lock():
    return {
        "purpose": "development",
        "scope": "cpu-direct-runtime-v1",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {name: version(name) for name in PACKAGES},
        "crop_runtime_sha256": json_digest(crop_runtime()),
        "code_sha256": code_digest(),
    }


def bounded_json(path, expected=None):
    with Path(path).open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536 or expected is not None and digest(raw) != expected:
        raise ValueError("Invalid pinned development artifact")
    value = json.loads(raw)
    json.dumps(value, allow_nan=False)
    return raw, value


@dataclass(frozen=True)
class CPUBundle:
    path: str
    sha256: str
    roots: tuple[str, ...]
    commit: str
    raw: bytes

    @classmethod
    def from_env(cls, commit):
        try:
            return cls.load(
                os.environ["AI_CPU_BUNDLE_FILE"],
                os.environ["AI_CPU_BUNDLE_SHA256"],
                tuple(os.environ["AI_ARTIFACT_ROOTS"].split(os.pathsep)),
                commit,
            )
        except (KeyError, OSError, ValueError, TypeError):
            raise RuntimeError("Invalid controlled CPU bundle configuration") from None

    @classmethod
    def load(cls, path, sha256, roots, commit="local-development"):
        if len(sha256) != 64 or not roots or any(not Path(p).is_absolute() for p in roots):
            raise ValueError("Explicit artifact roots and bundle SHA required")
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Absolute bundle path required")
        raw, value = bounded_json(path, sha256)
        validate("DevelopmentCPUBundle", value)
        result = cls(
            str(path.resolve()), sha256, tuple(str(Path(p).resolve()) for p in roots), commit, raw
        )
        result.paths()  # No large model read in the HTTP supervisor.
        return result

    @property
    def manifest(self):
        return json.loads(self.raw)

    def paths(self):
        paths = {}
        for name, ref in self.manifest["artifacts"].items():
            path = Path(ref["path"])
            resolved = path.resolve(strict=True)
            if (
                not path.is_absolute()
                or not resolved.is_file()
                or not any(resolved.is_relative_to(Path(root)) for root in self.roots)
            ):
                raise ValueError("Artifact outside controlled roots")
            paths[name] = resolved
        for prefix in ("ocr_det", "ocr_rec"):
            if (
                paths[prefix + "_model"].name != "inference.onnx"
                or paths[prefix + "_config"].name != "inference.yml"
                or paths[prefix + "_model"].parent != paths[prefix + "_config"].parent
            ):
                raise ValueError("OCR artifact paths must match the adapter")
        return paths

    @property
    def identity(self):
        value = self.manifest
        return {
            "service_commit": self.commit,
            "pipeline_version": value["pipeline_version"],
            "model_bundle_id": value["bundle_id"],
            "dictionary_version_id": value["dictionary_version_id"],
            "device_profile": "cpu",
            "purpose": "development",
            "is_simulated": False,
            "adapter_id": value["adapter_id"],
            "runtime_profile": value["runtime_profile"],
            "runtime_lock_sha256": value["artifacts"]["runtime_lock"]["sha256"],
            "model_checksum": self.sha256,
            "dictionary_sha256": value["artifacts"]["dictionary"]["sha256"],
            "detector_device": "cpu",
            "ocr_device": "cpu",
        }

    def verify(self):
        try:
            bounded_json(self.path, self.sha256)
            paths = self.paths()
            for name, path in paths.items():
                hasher = hashlib.sha256()
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        hasher.update(chunk)
                if hasher.hexdigest() != self.manifest["artifacts"][name]["sha256"]:
                    raise ValueError("Artifact digest changed")
            _, dictionary = bounded_json(paths["dictionary"], self.identity["dictionary_sha256"])
            validate("DevelopmentDictionary", dictionary)
            validate_dictionary(dictionary)
            if dictionary["dictionary_version_id"] != self.manifest["dictionary_version_id"]:
                raise ValueError("Dictionary identity mismatch")
            _, lock = bounded_json(paths["runtime_lock"], self.identity["runtime_lock_sha256"])
            validate("DevelopmentCPULock", lock)
            if lock != runtime_lock():
                raise ValueError("Runtime identity mismatch")
            validate("Version", self.identity)
            return self.identity
        except (OSError, ValueError, TypeError, KeyError):
            raise AdapterError(
                "MODEL_VERSION_UNAVAILABLE", "CPU bundle verification failed"
            ) from None

    def options(self):
        paths, value = self.paths(), self.manifest
        thresholds = value["thresholds"]
        return PipelineOptions(
            str(paths["detector_model"]),
            str(paths["ocr_det_model"].parent),
            str(paths["ocr_rec_model"].parent),
            str(paths["detector_config"]),
            str(paths["detector_preprocessor"]),
            thresholds["detection_min"],
            thresholds["ocr_min"],
            QualityThresholds(
                thresholds["blur_min"], thresholds["dark_min"], thresholds["glare_max"]
            ),
            threads=value["threads"],
            entity_min=thresholds["entity_min"],
        )
