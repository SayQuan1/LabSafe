"""Generate actual local CPU identity/lock; no downloading or production activation."""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from apps.ai_inference.bundle import CPUBundle, digest, runtime_lock
from packages.inference_protocol.chemical import RUNTIME_PROFILE, bounded_json
from packages.inference_protocol.chemical import dictionary as validate_dictionary
from packages.inference_protocol.contract import validate


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate(output, model, det_dir, rec_dir, *, commit="local-development", dictionary_file=None):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Use an empty local output directory; existing identities are immutable")
    model, det_dir, rec_dir = (Path(p).resolve() for p in (model, det_dir, rec_dir))
    dictionary_id = str(uuid4())
    dictionary = {"purpose": "development", "dictionary_version_id": dictionary_id, "entries": []}
    if dictionary_file is not None:
        with Path(dictionary_file).open("rb") as source:
            raw_dictionary = source.read(65537)
        dictionary = validate_dictionary(bounded_json(raw_dictionary.decode("utf-8")))
        dictionary_id = dictionary["dictionary_version_id"]
    lock = runtime_lock()
    validate("DevelopmentDictionary", dictionary)
    validate("DevelopmentCPULock", lock)
    write(output / "dictionary.json", dictionary)
    write(output / "runtime-lock.json", lock)
    paths = {
        "detector_model": model,
        "detector_config": model.parent / "config.json",
        "detector_preprocessor": model.parent / "preprocessor_config.json",
        "ocr_det_model": det_dir / "inference.onnx",
        "ocr_det_config": det_dir / "inference.yml",
        "ocr_rec_model": rec_dir / "inference.onnx",
        "ocr_rec_config": rec_dir / "inference.yml",
        "dictionary": output / "dictionary.json",
        "runtime_lock": output / "runtime-lock.json",
    }
    artifacts = {}
    import hashlib

    for name, path in paths.items():
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                hasher.update(chunk)
        artifacts[name] = {"path": str(path), "sha256": hasher.hexdigest()}
    value = {
        "purpose": "development",
        "bundle_id": str(uuid4()),
        "dictionary_version_id": dictionary_id,
        "pipeline_version": "vision-v1",
        "device_profile": "cpu",
        "adapter_id": "dfine-coco80-rgb-stretch-v1",
        "runtime_profile": RUNTIME_PROFILE,
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
    validate("DevelopmentCPUBundle", value)
    path = output / "bundle.json"
    write(path, value)
    sha = digest(path.read_bytes())
    roots = tuple(sorted({str(p.parent) for p in paths.values()}))
    bundle = CPUBundle.load(str(path), sha, roots, commit)
    bundle.verify()
    write(output / "expected-version.json", bundle.identity)
    return bundle


def main():
    parser = argparse.ArgumentParser()
    for name in ("output", "model", "det-dir", "rec-dir"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--commit", default="local-development")
    parser.add_argument("--dictionary", type=Path)
    args = parser.parse_args()
    bundle = generate(
        args.output,
        args.model,
        args.det_dir,
        args.rec_dir,
        commit=args.commit,
        dictionary_file=args.dictionary,
    )
    print(
        json.dumps(
            {
                "bundle_file": bundle.path,
                "bundle_sha256": bundle.sha256,
                "artifact_roots": bundle.roots,
                "expected_version_file": str(args.output.resolve() / "expected-version.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
