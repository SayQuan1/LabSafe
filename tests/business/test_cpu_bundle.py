"""Controlled metadata failures before any model/network activity."""

import copy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.bundle import CPUBundle, bounded_json, digest
from tools.ml.create_cpu_bundle import write


@pytest.fixture
def local_bundle(tmp_path):
    dictionary_id = str(uuid4())
    artifacts = {}
    for name in (
        "detector_model",
        "detector_config",
        "detector_preprocessor",
        "ocr_det_model",
        "ocr_det_config",
        "ocr_rec_model",
        "ocr_rec_config",
        "dictionary",
        "runtime_lock",
    ):
        directory = tmp_path / name.removesuffix("_model").removesuffix("_config")
        directory.mkdir(exist_ok=True)
        filename = (
            "inference.onnx"
            if name.startswith("ocr_") and name.endswith("_model")
            else ("inference.yml" if name.startswith("ocr_") else "artifact.json")
        )
        path = directory / filename
        path.write_bytes(name.encode())
        artifacts[name] = {"path": str(path), "sha256": digest(path.read_bytes())}
    value = {
        "purpose": "development",
        "bundle_id": str(uuid4()),
        "dictionary_version_id": dictionary_id,
        "pipeline_version": "vision-v1",
        "device_profile": "cpu",
        "adapter_id": "dfine-coco80-rgb-stretch-v1",
        "runtime_profile": "dfine-cpu-fp32-ocrv6smallcpu-chemical-v2",
        "artifacts": artifacts,
        "thresholds": {
            "detection_min": 0.4,
            "ocr_min": 0.6,
            "entity_min": 0.9,
            "blur_min": 80,
            "dark_min": 0.12,
            "glare_max": 0.3,
        },
        "threads": 2,
    }
    lock = {
        "purpose": "development",
        "scope": "cpu-direct-runtime-v1",
        "python": "3.11.4",
        "platform": "test",
        "packages": {
            k: "1"
            for k in (
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
        },
        "crop_runtime_sha256": "a" * 64,
        "code_sha256": "b" * 64,
    }
    write(
        Path(artifacts["dictionary"]["path"]),
        {"purpose": "development", "dictionary_version_id": dictionary_id, "entries": []},
    )
    write(Path(artifacts["runtime_lock"]["path"]), lock)
    for artifact in artifacts.values():
        artifact["sha256"] = digest(Path(artifact["path"]).read_bytes())
    return tmp_path, value, lock


def load(local_bundle, changes=None, roots=None):
    root, value, _ = local_bundle
    value = copy.deepcopy(value)
    if changes:
        changes(value)
    path = root / "bundle.json"
    write(path, value)
    return CPUBundle.load(str(path), digest(path.read_bytes()), roots or (str(root),))


def test_actual_identity_options_and_snapshot(local_bundle):
    bundle = load(local_bundle)
    with patch("apps.ai_inference.bundle.runtime_lock", return_value=local_bundle[2]):
        assert bundle.verify() == bundle.identity
    assert bundle.identity["is_simulated"] is False
    assert bundle.options().threads == 2
    changed = bundle.manifest
    changed["threads"] = 1
    assert bundle.manifest["threads"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("purpose", "production"),
        ("device_profile", "cuda"),
        ("adapter_id", "fixture-v1"),
        ("threads", 0),
    ],
)
def test_wrong_profile_rejected(local_bundle, field, value):
    with pytest.raises(ValueError):
        load(local_bundle, lambda v: v.update({field: value}))


def test_escape_root_and_wrong_ocr_filename_rejected(local_bundle):
    root = local_bundle[0]
    with pytest.raises(ValueError):
        load(local_bundle, roots=(str(root / "dictionary"),))
    path = root / "wrong.onnx"
    path.write_bytes(b"wrong")
    with pytest.raises(ValueError):
        load(local_bundle, lambda v: v["artifacts"]["ocr_det_model"].update(path=str(path)))


@pytest.mark.parametrize("name", ["detector_model", "dictionary", "runtime_lock", "ocr_rec_config"])
def test_changed_artifact_or_lock_prevents_load(local_bundle, name):
    bundle = load(local_bundle)
    Path(bundle.manifest["artifacts"][name]["path"]).write_bytes(b"changed")
    with patch("apps.ai_inference.bundle.runtime_lock", return_value=local_bundle[2]):
        with pytest.raises(AdapterError) as error:
            bundle.verify()
    assert error.value.code == "MODEL_VERSION_UNAVAILABLE"


def test_runtime_drift_or_manifest_drift_rejected(local_bundle):
    bundle = load(local_bundle)
    with patch("apps.ai_inference.bundle.runtime_lock", return_value={}):
        with pytest.raises(AdapterError):
            bundle.verify()
    Path(bundle.path).write_bytes(b"changed")
    with pytest.raises(AdapterError):
        bundle.verify()


def test_pinned_digest_and_small_json_cap(local_bundle):
    bundle = load(local_bundle)
    with pytest.raises(ValueError):
        CPUBundle.load(bundle.path, "0" * 64, bundle.roots)
    Path(bundle.path).write_bytes(b" " * 65537)
    with pytest.raises(ValueError):
        bounded_json(bundle.path)


def test_dictionary_wrong_id_even_with_correct_file_digest(local_bundle):
    root, value, lock = local_bundle
    artifact = value["artifacts"]["dictionary"]
    write(
        Path(artifact["path"]),
        {"purpose": "development", "dictionary_version_id": str(uuid4()), "entries": []},
    )
    artifact["sha256"] = digest(Path(artifact["path"]).read_bytes())
    bundle = load((root, value, lock))
    with patch("apps.ai_inference.bundle.runtime_lock", return_value=lock):
        with pytest.raises(AdapterError):
            bundle.verify()


def test_semantically_invalid_dictionary_rejected_with_correct_digest(local_bundle):
    root, value, lock = local_bundle
    from tests.evidence_helpers import synthetic_chemical_entries

    entries = synthetic_chemical_entries()
    entries[0]["cas"] = "64-17-6"
    artifact = value["artifacts"]["dictionary"]
    write(
        Path(artifact["path"]),
        {
            "purpose": "development",
            "dictionary_version_id": value["dictionary_version_id"],
            "entries": entries,
        },
    )
    artifact["sha256"] = digest(Path(artifact["path"]).read_bytes())
    bundle = load((root, value, lock))
    with patch("apps.ai_inference.bundle.runtime_lock", return_value=lock):
        with pytest.raises(AdapterError):
            bundle.verify()
