"""Development runner: fixed-version analysis GET into the existing CPU pipeline.

The report is not an InferenceResult or a claim of an approved model bundle.
"""

import copy
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.analysis import AnalysisReader
from apps.ai_inference.cpu import run_bounded_child
from apps.ai_inference.inputs import verify_image_inputs
from apps.ai_inference.pipeline_cpu import _compute


def _child(channel, options, payload, settings, deadline):
    try:
        reader = AnalysisReader(settings)
        downloaded_ms = 0

        def decode(reference):
            nonlocal downloaded_ms
            started = time.monotonic()
            result = reader.decode(
                reference, payload["tenant_id"], payload["laboratory_id"], deadline
            )
            downloaded_ms += round((time.monotonic() - started) * 1000)
            return result

        report = _compute(
            options,
            payload["image_refs"],
            payload["run_id"],
            decoder=decode,
            image_ids=[ref["image_id"] for ref in payload["image_refs"]],
        )
        report["schema_version"] = "cpu-pipeline-analysis-v1"
        report["input_source"] = "fixed-version-analysis"
        report["request_hash"] = payload["request_hash"]
        report["timing_ms"]["download_and_decode_ms"] = downloaded_ms
        for row, ref in zip(report["images"], payload["image_refs"]):
            row["object_version"] = ref["object_version"]
        channel.send(("result", report))
    except AdapterError as error:
        channel.send(("error", error.code))
    except MemoryError:
        channel.send(("error", "MODEL_OOM"))
    except Exception:
        channel.send(("error", "MODEL_ERROR"))
    finally:
        channel.close()


def run_analysis_pipeline(options, payload, settings):
    """Validate before any network/model work; one deadline includes child startup."""
    if sys.version_info[:2] != (3, 11) or os.environ.get("APP_ENV") not in {"dev", "test"}:
        raise AdapterError("VALIDATION_ERROR", "Development CPU runtime requires Python 3.11")
    options.validate()
    settings.validate()
    payload = copy.deepcopy(payload)
    verify_image_inputs(payload, settings.allowed_tenants)
    if payload["device_profile"] != "cpu":
        raise AdapterError("VALIDATION_ERROR", "CPU-only execution is required")
    remaining = (
        datetime.fromisoformat(payload["deadline_at"].upper().replace("Z", "+00:00"))
        - datetime.now(timezone.utc)
    ).total_seconds()
    budget = min(remaining, options.timeout_seconds, 10 if options.quality_only else 180)
    if budget <= 0:
        raise AdapterError("AI_TIMEOUT", "Request deadline has expired")
    deadline = time.monotonic() + budget
    return run_bounded_child(
        budget, _child, replace(options, timeout_seconds=budget), payload, settings, deadline
    )
