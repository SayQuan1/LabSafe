"""Cancel and hard-bound the independent image/storage runtime to 20 seconds."""

import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packages.domain.inference_evidence import InferenceDerivative, validate_derivatives
from packages.domain.inference_execution import InferenceLeaseLost, request_payload, validate_result
from packages.domain.security import ServiceError

EVIDENCE_SECONDS = 20
ERRORS = {
    "DEPENDENCY_UNAVAILABLE",
    "STAGE_TIMEOUT",
    "INTERNAL_ERROR",
    "SCHEMA_MISMATCH",
    "HASH_MISMATCH",
    "OBJECT_NOT_FOUND",
    "IMAGE_INVALID",
    "IMAGE_TOO_LARGE",
    "MODEL_ERROR",
}


class BoundedEvidencePrepare:
    def __init__(self, executable=None):
        self.executable = executable or os.environ.get("WORKER_EVIDENCE_PYTHON")

    def __call__(self, lease, result, cancelled):
        validate_result(result, lease)
        if not result["crops"]:
            return ()
        if not self.executable or not Path(self.executable).is_file():
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Evidence runtime is required")
        source = request_payload(lease, datetime.now(timezone.utc) + timedelta(seconds=20))
        raw = json.dumps({"source": source, "result": result}, allow_nan=False).encode()
        if len(raw) > 2 * 1024 * 1024:
            raise ServiceError("SCHEMA_MISMATCH", 502, "Evidence input exceeds limit")
        # No database/broker/AI credentials inherited by this dedicated runtime.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("DATABASE_", "CELERY_", "REDIS_", "AI_", "BROKER_"))
            and key not in {"S3_ACCESS_KEY_FILE", "S3_SECRET_KEY_FILE"}
        }
        environment.update(
            PYTHONDONTWRITEBYTECODE="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1"
        )
        child = None
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="labsafe-ocr-evidence-") as directory:
            input_path, output_path = (
                Path(directory) / "input.json",
                Path(directory) / "output.json",
            )
            input_path.write_bytes(raw)
            try:
                if cancelled.is_set():
                    raise InferenceLeaseLost()
                child = subprocess.Popen(
                    [
                        str(Path(self.executable).resolve()),
                        "-B",
                        "-m",
                        "apps.worker.evidence_child",
                        str(input_path),
                        str(output_path),
                    ],
                    cwd=Path(__file__).resolve().parents[2],
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                while child.poll() is None:
                    if cancelled.wait(0.05):
                        raise InferenceLeaseLost()
                    if time.monotonic() - started >= EVIDENCE_SECONDS:
                        raise ServiceError("STAGE_TIMEOUT", 504, "Evidence preparation timed out")
                if cancelled.is_set():
                    raise InferenceLeaseLost()
                if time.monotonic() - started >= EVIDENCE_SECONDS:
                    raise ServiceError("STAGE_TIMEOUT", 504, "Evidence preparation timed out")
                if (
                    child.returncode
                    or not output_path.is_file()
                    or output_path.stat().st_size > 256 * 1024
                ):
                    raise ServiceError("INTERNAL_ERROR", 500, "Evidence runtime failed")
                output = json.loads(output_path.read_text(encoding="utf-8"))
                if "error" in output:
                    code = output["error"] if output["error"] in ERRORS else "INTERNAL_ERROR"
                    raise ServiceError(code, 503, "Evidence preparation failed")
                artifacts = tuple(InferenceDerivative(**row) for row in output["artifacts"])
                validate_derivatives(lease, result, artifacts)
                return artifacts
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=2)
