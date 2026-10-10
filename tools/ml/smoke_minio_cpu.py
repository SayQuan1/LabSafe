"""Actual local MinIO IAM and CPU HTTP -> isolated Worker, using user-provided photos."""

import argparse
import copy
import hashlib
import io
import json
import os
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5

from PIL import Image, ImageDraw, ImageFont, ImageOps

from apps.worker.inference_evidence import BoundedEvidencePrepare
from packages.application.inference_execution import AIRequestError, InferenceHTTPClient
from packages.domain.inference_execution import validate_result
from packages.inference_protocol.chemical import context_for_result, extract_entities
from packages.inference_protocol.fields import extract_fields
from packages.inference_protocol.hashing import request_hash
from packages.storage.s3 import BUCKET
from tools.ml.analysis_fixture import LAB, TENANT, payload_for
from tools.ml.minio_fixture import free_port, minio_store
from tools.ml.smoke_cpu_http import lease_for


def png(path):
    with Image.open(path) as original:
        with ImageOps.exif_transpose(original) as upright:
            with upright.convert("RGB") as image:
                stream = io.BytesIO()
                image.save(stream, format="PNG")
                return stream.getvalue()


def main():
    parser = argparse.ArgumentParser()
    for name in (
        "minio",
        "ai-python",
        "evidence-python",
        "model",
        "det-dir",
        "rec-dir",
        "font",
        "output",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument(
        "--synthetic-dictionary",
        action="store_true",
        help="Explicit test-only name/CAS mapping; never a chemistry data source",
    )
    args = parser.parse_args()
    photos = [(png(path), hashlib.sha256(path.read_bytes()).hexdigest()) for path in args.input]
    # A separate synthetic label supplies an explicit positive evidence probe.
    with Image.new("RGB", (800, 400), (180, 180, 180)) as image:
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(str(args.font), 42)
        for i, text in enumerate(("NAME", "Alpha", "CAS 64-17-5")):
            draw.text((40, 50 + 100 * i), text, font=font, fill="black")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        synthetic = stream.getvalue()
    summaries, probe_summary = [], None
    with tempfile.TemporaryDirectory(prefix="labsafe-minio-cpu-") as directory:
        root = Path(directory)
        dictionary_args = []
        if args.synthetic_dictionary:
            dictionary_file = root / "synthetic-dictionary.json"
            dictionary_file.write_text(
                json.dumps(
                    {
                        "purpose": "development",
                        "dictionary_version_id": str(uuid4()),
                        "entries": [
                            {
                                "entity_id": str(uuid4()),
                                "canonical_name": "Alpha",
                                "aliases": [],
                                "cas": "64-17-5",
                                "source": "synthetic test-only mapping; NOT chemistry/safety data",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            dictionary_args = ["--dictionary", str(dictionary_file)]
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
                *dictionary_args,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        metadata = json.loads(generated.stdout)
        identity = json.loads(Path(metadata["expected_version_file"]).read_text(encoding="utf-8"))
        with minio_store(args.minio, TENANT, LAB) as (admin, ai, worker, iam_checks):
            for name, value in (
                ("ai-access", ai.access_key),
                ("ai-secret", ai.secret_key),
                ("worker-access", worker.access_key),
                ("worker-secret", worker.secret_key),
                ("token", secrets.token_urlsafe(32)),
            ):
                (root / name).write_text(value, encoding="ascii")
            port = free_port()
            env = {
                **os.environ,
                "APP_ENV": "test",
                "AI_MODE": "cpu",
                "AI_TOKEN_FILE": str(root / "token"),
                "AI_ALLOWED_TENANTS": TENANT,
                "AI_CPU_BUNDLE_FILE": metadata["bundle_file"],
                "AI_CPU_BUNDLE_SHA256": metadata["bundle_sha256"],
                "AI_ARTIFACT_ROOTS": os.pathsep.join(metadata["artifact_roots"]),
                "AI_S3_ENDPOINT": ai.endpoint,
                "AI_S3_ALLOWED_ENDPOINTS": ai.endpoint,
                "AI_S3_ACCESS_KEY_FILE": str(root / "ai-access"),
                "AI_S3_SECRET_KEY_FILE": str(root / "ai-secret"),
                "AI_INFERENCE_PORT": str(port),
                "SMOKE_CONTROL_DIRECTORY": directory,
                "SERVICE_COMMIT": "local-development",
                "AI_MAX_INFLIGHT": "1",
            }
            worker_env = {
                "APP_ENV": "test",
                "PUBLIC_ORIGIN": "https://labsafe.test",
                "S3_ENDPOINT": ai.endpoint,
                "S3_PUBLIC_ENDPOINT": "https://labsafe.test",
                "S3_WORKER_ACCESS_KEY_FILE": str(root / "worker-access"),
                "S3_WORKER_SECRET_KEY_FILE": str(root / "worker-secret"),
                "WORKER_EVIDENCE_PYTHON": str(args.evidence_python),
            }
            client = InferenceHTTPClient(
                f"http://127.0.0.1:{port}", (root / "token").read_text(), expected_version=identity
            )
            process = None
            with (root / "http.log").open("wb") as log:
                try:
                    process = subprocess.Popen(
                        [str(args.ai_python), "-B", "-m", "tools.ml.http_cpu_fixture"],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    end = time.monotonic() + 120
                    while time.monotonic() < end:
                        if process.poll() is not None:
                            raise RuntimeError("CPU HTTP startup failed")
                        try:
                            if client._exchange("ready", time.monotonic() + 1)["status"] == "ready":
                                break
                        except AIRequestError:
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("CPU startup timeout")
                    pid = json.loads((root / "pid.json").read_text())["pid"]
                    for index, (raw, source_sha) in enumerate([*photos, (synthetic, None)]):
                        source = payload_for([raw])
                        for key in (
                            "model_bundle_id",
                            "model_checksum",
                            "dictionary_version_id",
                            "dictionary_sha256",
                            "pipeline_version",
                            "device_profile",
                        ):
                            source[key] = identity[key]
                        ref = source["image_refs"][0]
                        ref["object_version"] = admin.put_object(
                            Bucket=BUCKET, Key=ref["object_key"], Body=raw, ContentType="image/png"
                        )["VersionId"]
                        # Replace latest bytes: all inference and Worker reads must still use V1.
                        latest = admin.put_object(
                            Bucket=BUCKET,
                            Key=ref["object_key"],
                            Body=b"synthetic-new-latest-invalid-png",
                            ContentType="image/png",
                        )["VersionId"]
                        assert latest != ref["object_version"]
                        source["request_hash"] = request_hash(source)
                        source["deadline_at"] = (
                            datetime.now(timezone.utc) + timedelta(seconds=200)
                        ).isoformat()
                        quality, result = client.quality(source), client.runs(source)
                        assert quality["quality"] == result["quality"]
                        value = lease_for(source)
                        validate_result(result, value)
                        with patch.dict(os.environ, worker_env):
                            artifacts = BoundedEvidencePrepare()(value, result, Event())
                            assert artifacts == BoundedEvidencePrepare()(value, result, Event())
                        for artifact in artifacts:
                            response = admin.get_object(
                                Bucket=BUCKET,
                                Key=artifact.object_key,
                                VersionId=artifact.object_version,
                            )
                            try:
                                assert (
                                    hashlib.sha256(response["Body"].read()).hexdigest()
                                    == artifact.sha256
                                )
                            finally:
                                response["Body"].close()
                        assert json.loads((root / "pid.json").read_text())["pid"] == pid
                        if source_sha is not None:
                            summaries.append(
                                {
                                    "photo_index": index + 1,
                                    "source_sha256": source_sha,
                                    "quality": result["quality"][0],
                                    "outcome": result["outcome"],
                                    "detections": len(result["detections"]),
                                    "bottles": sum(
                                        d["type"] == "bottle" for d in result["detections"]
                                    ),
                                    "text_regions": len(result["text_regions"]),
                                    "associated_text_regions": sum(
                                        t["detection_id"] is not None
                                        for t in result["text_regions"]
                                    ),
                                    "detection_classes": {
                                        kind: sum(d["type"] == kind for d in result["detections"])
                                        for kind in sorted(
                                            {d["type"] for d in result["detections"]}
                                        )
                                    },
                                    "fields": len(result["ocr_fields"]),
                                    "entity_groups": len(result["entities"]),
                                    "resolution_counts": {
                                        status: sum(
                                            e["resolution"] == status for e in result["entities"]
                                        )
                                        for status in ("resolved", "candidate", "unknown")
                                    },
                                    "uploaded_crops": len(artifacts),
                                }
                            )
                        else:
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
                            ]
                            for row in probe["text_regions"] + probe["crops"]:
                                row["detection_id"] = probe["detections"][0]["detection_id"]
                            data, thresholds = context_for_result(probe)
                            probe["ocr_fields"] = extract_fields(probe["text_regions"], data)
                            probe["entities"] = extract_entities(
                                probe["ocr_fields"],
                                data,
                                thresholds["ocr_min"],
                                thresholds["entity_min"],
                            )
                            expected_resolution = (
                                "resolved" if args.synthetic_dictionary else "unknown"
                            )
                            assert (
                                len(probe["ocr_fields"]) == 2
                                and probe["entities"][0]["resolution"] == expected_resolution
                            )
                            validate_result(probe, value)
                            with patch.dict(os.environ, worker_env):
                                positive = BoundedEvidencePrepare()(value, probe, Event())
                            assert len(positive) == len(artifacts) == 3
                            probe_summary = {
                                "detection_kind": "explicit-synthetic-bottle",
                                "ocr_kind": "real-official-onnx",
                                "is_simulated": True,
                                "fields": 2,
                                "entity_groups": 1,
                                "resolution": expected_resolution,
                                "uploaded_crops": 3,
                            }
                finally:
                    if process is not None:
                        (root / "stop").write_text("")
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                            raise RuntimeError("CPU HTTP failed to shut down")
            shutdown = json.loads((root / "closed.json").read_text())
            assert all(shutdown.values())
        report = {
            "status": "PASS",
            "executed_at_utc": datetime.now(timezone.utc).isoformat(),
            "storage_kind": "real-isolated-minio",
            "server_version": subprocess.run(
                [str(args.minio), "--version"], capture_output=True, text=True, check=True
            ).stdout.splitlines()[0],
            "inference_kind": "real-official-onnx-cpu-http",
            "identity": identity,
            "dictionary_kind": "synthetic-test-only"
            if args.synthetic_dictionary
            else "empty-development",
            "images": summaries,
            "positive_probe": probe_summary,
            "iam_denials": iam_checks,
            "original_version_used_after_latest_overwrite": True,
            "derivative_version_sha_and_reuse": True,
            "resident_pid_reused": True,
            "shutdown": shutdown,
            "limitations": [
                "No annotated accuracy evaluation or threshold calibration",
                "No production chemistry dictionary/safety classification",
                "No database commit in smoke; MySQL fence tested separately",
                "Loopback HTTP only; no nginx/public presigned URL/TLS or production deployment",
            ],
        }
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
