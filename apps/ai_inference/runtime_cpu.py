"""One resident numerical process; JSON IPC only, no ORM or business credentials."""

import json
import os
import time
from dataclasses import replace

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.analysis import AnalysisReader
from apps.ai_inference.pipeline_cpu import _compute, load_models
from packages.inference_protocol.chemical import bounded_json
from packages.inference_protocol.contract import validate
from packages.inference_protocol.evidence import validate_closure

MAX_REQUEST = 64 * 1024
MAX_RESULT = 2 * 1024 * 1024


def send(channel, value):
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_RESULT:
        raise AdapterError("MODEL_ERROR", "Inference response exceeds capacity")
    channel.send_bytes(raw)


def wire_result(report, payload, identity, download_ms, extraction_context=None):
    result = {
        key: payload[key]
        for key in ("run_id", "attempt_id", "fencing_token", "tenant_id", "request_hash")
    }
    result.update(
        {
            key: identity[key]
            for key in (
                "model_bundle_id",
                "model_checksum",
                "dictionary_version_id",
                "dictionary_sha256",
                "pipeline_version",
            )
        }
    )
    result.update(
        execution_identity=identity,
        input_hashes=[
            {"image_id": row["image_id"], "sha256": row["input_sha256"]} for row in report["images"]
        ],
        outcome="facts_ready" if report["outcome"] == "quality_passed" else report["outcome"],
        quality=[
            {
                "image_id": row["image_id"],
                "status": "pass" if row["quality"]["passed"] else "needs_retake",
                **{
                    key: row["quality"][key]
                    for key in ("reasons", "blur_score", "brightness", "glare_ratio")
                },
            }
            for row in report["images"]
        ],
        detections=[d for row in report["images"] for d in row["detections"]],
        text_regions=report["text_regions"],
        crops=report["crops"],
        ocr_fields=report["ocr_fields"],
        entities=report["entities"],
        relations=[],
        timing_ms={
            "download_ms": download_ms,
            "quality_ms": report["timing_ms"]["quality_ms"],
            "detection_ms": sum(row["detection_ms"] for row in report["images"]),
            "ocr_ms": sum(row["ocr_ms"] for row in report["images"]),
            "normalization_ms": report["timing_ms"].get("association_ms", 0)
            + report["timing_ms"].get("fields_ms", 0),
            "total_ms": report["timing_ms"]["total_ms"],
        },
    )
    if extraction_context is not None:
        result["extraction_context"] = extraction_context
    validate("InferenceResult", result)
    validate_closure(result, [ref["image_id"] for ref in payload["image_refs"]])
    return result


def child_main(channel, bundle, analysis):
    try:
        # Explicit readonly AnalysisSettings is the child's only storage capability.
        for name in list(os.environ):
            if name.startswith(
                ("DB_", "DATABASE_", "REDIS_", "CELERY_", "S3_", "AWS_", "API_")
            ) or name in {
                "AI_TOKEN_FILE",
                "AI_EXPECTED_VERSION_FILE",
                "AI_S3_ACCESS_KEY_FILE",
                "AI_S3_SECRET_KEY_FILE",
            }:
                os.environ.pop(name, None)
        identity = bundle.verify()
        extraction_context = {
            "bundle_json": bundle.raw.decode("utf-8"),
            "dictionary_json": bundle.paths()["dictionary"].read_bytes().decode("utf-8"),
        }
        dictionary = bounded_json(extraction_context["dictionary_json"])
        options = bundle.options()
        options.validate()
        models = load_models(options)
        # Recheck bytes/runtime after loading; adapter sessions already use verified model bytes.
        bundle.verify()
        send(channel, {"kind": "loaded", "identity": identity})
        reader = AnalysisReader(analysis)
        while True:
            command = json.loads(channel.recv_bytes(MAX_REQUEST))
            payload, deadline = command["payload"], command["deadline"]
            downloaded_ms = 0

            def decode(ref):
                nonlocal downloaded_ms
                started = time.monotonic()
                decoded = reader.decode(
                    ref, payload["tenant_id"], payload["laboratory_id"], deadline
                )
                downloaded_ms += round((time.monotonic() - started) * 1000)
                return decoded

            report = _compute(
                replace(options, quality_only=command["stage"] == "quality"),
                payload["image_refs"],
                payload["run_id"],
                decoder=decode,
                image_ids=[r["image_id"] for r in payload["image_refs"]],
                models=models,
                dictionary=dictionary,
            )
            if time.monotonic() >= deadline:
                raise AdapterError("AI_TIMEOUT", "CPU deadline exceeded")
            send(
                channel,
                {
                    "kind": "result",
                    "value": wire_result(
                        report, payload, identity, downloaded_ms, extraction_context
                    ),
                },
            )
    except AdapterError as error:
        send(channel, {"kind": "error", "code": error.code})
    except MemoryError:
        send(channel, {"kind": "error", "code": "MODEL_OOM"})
    except (EOFError, BrokenPipeError, OSError):
        pass
    except Exception:
        send(channel, {"kind": "error", "code": "MODEL_ERROR"})
    finally:
        channel.close()
