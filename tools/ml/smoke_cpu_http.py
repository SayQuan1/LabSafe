"""Real official CPU HTTP -> Worker evidence, with explicit simulated storage."""

import argparse
import copy
import hashlib
import io
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import UUID, uuid5

from PIL import Image, ImageDraw, ImageFont, ImageOps

from apps.worker.inference_evidence import BoundedEvidencePrepare
from packages.application.inference_execution import AIRequestError, InferenceHTTPClient
from packages.domain.inference_execution import (
    InferenceImage,
    InferenceInput,
    InferenceLease,
    validate_result,
)
from packages.inference_protocol.association import ALGORITHM_ID, select_bottle
from packages.inference_protocol.chemical import context_for_result, extract_entities
from packages.inference_protocol.fields import ALGORITHM_ID as FIELDS_ID
from packages.inference_protocol.fields import extract_fields
from packages.inference_protocol.hashing import request_hash
from tools.ml.analysis_fixture import payload_for
from tools.ml.evidence_fixture import evidence_store
from tools.ml.smoke_worker_evidence import worker_environment


def lease_for(source):
    value = InferenceInput(
        **{
            key: source[key]
            for key in (
                "tenant_id",
                "laboratory_id",
                "item_id",
                "run_id",
                "submission_revision",
                "model_bundle_id",
                "model_checksum",
                "dictionary_version_id",
                "dictionary_sha256",
                "pipeline_version",
                "device_profile",
            )
        },
        images=tuple(InferenceImage(**row) for row in source["image_refs"]),
    )
    return InferenceLease(
        value.tenant_id,
        source["item_id"],
        source["item_id"],
        source["attempt_id"],
        source["fencing_token"],
        0,
        1,
        value,
    )


def main():
    parser = argparse.ArgumentParser()
    for name in ("ai-python", "evidence-python", "model", "det-dir", "rec-dir", "font", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument(
        "--input", type=Path, help="Optional user-provided photo; never copied into Git"
    )
    args = parser.parse_args()
    raws = []
    expected_text = ["NAME", "Ethanol", "EXP 2027-12-31", "乙醇"]
    font = ImageFont.truetype(str(args.font), 42)
    for size in ((800, 500), (500, 800)):
        with Image.new("RGB", size, (180, 180, 180)) as image:
            draw = ImageDraw.Draw(image)
            for index, text in enumerate(expected_text):
                draw.text((40, 50 + 100 * index), text, font=font, fill="black")
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            raws.append(stream.getvalue())
    photo_sha = None
    if args.input:
        photo_sha = hashlib.sha256(args.input.read_bytes()).hexdigest()
        with Image.open(args.input) as image:
            upright = ImageOps.exif_transpose(image)
            try:
                with upright.convert("RGB") as rgb:
                    stream = io.BytesIO()
                    rgb.save(stream, format="PNG")
                    raws.append(stream.getvalue())
            finally:
                upright.close()
    source = payload_for(raws)
    with tempfile.TemporaryDirectory(prefix="labsafe-http-smoke-") as directory:
        root = Path(directory)
        generated = subprocess.run(
            [
                str(args.ai_python),
                "-B",
                "-m",
                "tools.ml.create_cpu_bundle",
                "--output",
                str(root / "bundle"),
                "--model",
                str(args.model),
                "--det-dir",
                str(args.det_dir),
                "--rec-dir",
                str(args.rec_dir),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = json.loads(generated.stdout)
        identity = json.loads(Path(metadata["expected_version_file"]).read_text(encoding="utf-8"))
        for key in (
            "model_bundle_id",
            "model_checksum",
            "dictionary_version_id",
            "dictionary_sha256",
            "pipeline_version",
            "device_profile",
        ):
            source[key] = identity[key]
        source["request_hash"] = request_hash(source)
        source["deadline_at"] = (datetime.now(timezone.utc) + timedelta(seconds=200)).isoformat()
        with evidence_store(source, raws) as (settings, uploads, seen):
            token = "synthetic-http-smoke-token-at-least-32bytes"
            (root / "token").write_text(token, encoding="ascii")
            (root / "ai-access").write_text(settings.access_key, encoding="ascii")
            (root / "ai-secret").write_text(settings.secret_key, encoding="ascii")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = {
                **os.environ,
                "APP_ENV": "test",
                "AI_MODE": "cpu",
                "AI_TOKEN_FILE": str(root / "token"),
                "AI_ALLOWED_TENANTS": source["tenant_id"],
                "AI_CPU_BUNDLE_FILE": metadata["bundle_file"],
                "AI_CPU_BUNDLE_SHA256": metadata["bundle_sha256"],
                "AI_ARTIFACT_ROOTS": os.pathsep.join(metadata["artifact_roots"]),
                "AI_S3_ENDPOINT": settings.endpoint,
                "AI_S3_ALLOWED_ENDPOINTS": settings.endpoint,
                "AI_S3_ACCESS_KEY_FILE": str(root / "ai-access"),
                "AI_S3_SECRET_KEY_FILE": str(root / "ai-secret"),
                "AI_INFERENCE_PORT": str(port),
                "SMOKE_CONTROL_DIRECTORY": directory,
                "SERVICE_COMMIT": "local-development",
                "AI_MAX_INFLIGHT": "1",
            }
            client = InferenceHTTPClient(
                f"http://127.0.0.1:{port}", token, expected_version=identity
            )
            with (root / "service.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    [str(args.ai_python), "-B", "-m", "tools.ml.http_cpu_fixture"],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                try:
                    end = time.monotonic() + 120
                    health_seen = False
                    while time.monotonic() < end:
                        if process.poll() is not None:
                            raise AssertionError("CPU HTTP process exited during startup")
                        try:
                            client._exchange("health", time.monotonic() + 1)
                            client._preflight(source, time.monotonic() + 1)
                            break
                        except AIRequestError as error:
                            if error.code == "MODEL_NOT_READY":
                                health_seen = True
                            time.sleep(0.05)
                    else:
                        raise AssertionError("Real CPU failed to become ready")
                    pid = json.loads((root / "pid.json").read_text())["pid"]
                    assert health_seen
                    # Unknown pin is rejected without an analysis GET.
                    wrong = dict(source, model_checksum="0" * 64)
                    try:
                        client.quality(wrong)
                    except AIRequestError as error:
                        assert error.code == "MODEL_VERSION_UNAVAILABLE"
                    else:
                        raise AssertionError("Unknown CPU identity accepted")
                    assert not seen
                    quality = client.quality(source)
                    result = client.runs(source)
                    replay = client.runs(source)
                    assert quality["quality"] == result["quality"] == replay["quality"]
                    assert result["text_regions"] == replay["text_regions"]
                    assert result["ocr_fields"] == replay["ocr_fields"]
                    assert result["ocr_fields"] == extract_fields(result["text_regions"])
                    assert all(
                        line["detection_id"]
                        == select_bottle(line["quad"], line["image_id"], result["detections"])
                        for line in result["text_regions"]
                    )
                    assert result["execution_identity"] == identity and not identity["is_simulated"]
                    assert [
                        line["raw_text"]
                        for line in result["text_regions"]
                        if line["image_id"] in {r["image_id"] for r in source["image_refs"][:2]}
                    ] == expected_text * 2
                    assert json.loads((root / "pid.json").read_text())["pid"] == pid
                    lease = lease_for(source)
                    validate_result(result, lease)
                    with patch.dict(
                        os.environ,
                        worker_environment(directory, settings.endpoint, args.evidence_python),
                    ):
                        artifacts = BoundedEvidencePrepare()(lease, result, Event())
                        assert artifacts == BoundedEvidencePrepare()(lease, result, Event())
                    assert len(artifacts) == len(uploads) == len(result["crops"])
                    # Separate positive field probe: real OCR/crop pixels, EXPLICIT synthetic
                    # full-frame bottle detections, simulated result. Not model accuracy.
                    probe = copy.deepcopy(result)
                    probe["execution_identity"]["is_simulated"] = True
                    probe["detections"] = [
                        {
                            "detection_id": str(
                                uuid5(UUID(source["run_id"]), f"{ref['image_id']}:bottle:0")
                            ),
                            "image_id": ref["image_id"],
                            "parent_detection_id": None,
                            "class_id": 39,
                            "type": "bottle",
                            "bbox": [0, 0, 1, 1],
                            "confidence": 0.9,
                        }
                        for ref in source["image_refs"][:2]
                    ]
                    for row in probe["text_regions"] + probe["crops"]:
                        row["detection_id"] = select_bottle(
                            row["quad"], row["image_id"], probe["detections"]
                        )
                    probe["ocr_fields"] = extract_fields(probe["text_regions"])
                    dictionary, thresholds = context_for_result(probe)
                    probe["entities"] = extract_entities(
                        probe["ocr_fields"],
                        dictionary,
                        thresholds["ocr_min"],
                        thresholds["entity_min"],
                    )
                    assert len(probe["ocr_fields"]) == 4
                    assert sum(len(f["source_lines"]) == 2 for f in probe["ocr_fields"]) == 2
                    validate_result(probe, lease)
                    with patch.dict(
                        os.environ,
                        worker_environment(directory, settings.endpoint, args.evidence_python),
                    ):
                        probe_artifacts = BoundedEvidencePrepare()(lease, probe, Event())
                    assert {a.crop_id for a in probe_artifacts} == {a.crop_id for a in artifacts}
                    assert all(
                        f["normalized_text"] in ("Ethanol", "2027-12-31")
                        for f in probe["ocr_fields"]
                    )
                    # Actual resident child death must withdraw ready and reload the same pin.
                    (root / "crash").write_text("", encoding="ascii")
                    time.sleep(0.2)
                    try:
                        client._exchange("ready", time.monotonic() + 1)
                    except AIRequestError as error:
                        assert error.code == "MODEL_NOT_READY"
                    else:
                        raise AssertionError("Dead resident child stayed ready")
                    end = time.monotonic() + 125
                    while time.monotonic() < end:
                        try:
                            client._preflight(source, time.monotonic() + 1)
                            break
                        except AIRequestError:
                            time.sleep(0.1)
                    else:
                        raise AssertionError("Real child did not recover")
                    recovered_pid = json.loads((root / "pid.json").read_text())["pid"]
                    assert recovered_pid != pid
                    source["deadline_at"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=200)
                    ).isoformat()
                    assert client.quality(source)["quality"] == quality["quality"]
                finally:
                    (root / "stop").write_text("", encoding="ascii")
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        # Test-only emergency cleanup, including the spawned numerical process.
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                check=False,
                                capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW,
                            )
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        raise AssertionError("Graceful CPU HTTP shutdown failed")
                closed = json.loads((root / "closed.json").read_text())
                assert all(closed.values())
        report = {
            "status": "PASS",
            "executed_at_utc": datetime.now(timezone.utc).isoformat(),
            "inference_kind": "real-official-onnx-cpu-http",
            "input_kind": "synthetic-rgb-pngs-and-user-photo"
            if args.input
            else "synthetic-rgb-pngs",
            "photo_source_sha256": photo_sha,
            "storage_kind": "versioned-loopback-s3-simulator",
            "identity": identity,
            "images": len(raws),
            "text_regions": len(result["text_regions"]),
            "uploaded_crops": len(artifacts),
            "fields_algorithm": FIELDS_ID,
            "ocr_fields": len(result["ocr_fields"]),
            "fields_recomputed_by_worker": True,
            "positive_field_probe": {
                "detection_kind": "explicit-synthetic-full-frame-bottles",
                "is_simulated": True,
                "ocr_kind": "real-official-onnx",
                "fields": len(probe["ocr_fields"]),
                "two_line_fields": 2,
                "all_source_crops_rebuilt": True,
            },
            "per_image": [
                {
                    "input_kind": "user-photo" if index == 2 else "synthetic-label",
                    "quality": result["quality"][index],
                    "detections": sum(
                        d["image_id"] == ref["image_id"] for d in result["detections"]
                    ),
                    "bottles": sum(
                        d["image_id"] == ref["image_id"] and d["type"] == "bottle"
                        for d in result["detections"]
                    ),
                    "text_regions": sum(
                        t["image_id"] == ref["image_id"] for t in result["text_regions"]
                    ),
                    "associated_regions": sum(
                        t["image_id"] == ref["image_id"] and t["detection_id"] is not None
                        for t in result["text_regions"]
                    ),
                    "fields": sum(f["image_id"] == ref["image_id"] for f in result["ocr_fields"]),
                }
                for index, ref in enumerate(source["image_refs"])
            ],
            "association_algorithm": ALGORITHM_ID,
            "associated_regions": sum(
                line["detection_id"] is not None for line in result["text_regions"]
            ),
            "unassociated_regions": sum(
                line["detection_id"] is None for line in result["text_regions"]
            ),
            "bottle_detections": sum(row["type"] == "bottle" for row in result["detections"]),
            "association_recomputed_by_worker": True,
            "health_during_startup": health_seen,
            "resident_pid_reused": True,
            "unknown_pin_no_analysis_get": True,
            "quality_runs_replay_equal": True,
            "worker_exact_version_reuse": True,
            "crash_ready_withdrawn_and_reloaded": True,
            "shutdown": closed,
            "limitations": [
                "No database commit in this smoke; MySQL tested separately",
                "No live S3/IAM/TLS; one user photo is an engineering probe, "
                "not annotated accuracy evaluation",
                "No semantic facts or production approval",
                "Synthetic tags may contain no detected bottles; positive association uses "
                "explicit synthetic boxes and real pixels in separate tests",
            ],
        }
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
