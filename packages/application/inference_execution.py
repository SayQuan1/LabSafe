"""Worker-side inference orchestration with bounded, injectable AI RPC."""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from uuid import uuid4

from packages.application.dispatch import transaction
from packages.domain.inference_execution import (
    InferenceInvalidResult,
    InferenceLeaseLost,
    request_payload,
    validate_result,
)
from packages.inference_protocol.contract import validate
from packages.persistence import inference_execution
from packages.shared.environment import development_environment


class AIRequestError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class InferenceHTTPClient:
    """Small stdlib client; tests may inject an object with quality/runs methods."""

    def __init__(self, endpoint=None, token=None, timeout=200):
        self.endpoint = (
            endpoint or os.environ.get("AI_INFERENCE_URL", "http://127.0.0.1:8001")
        ).rstrip("/")
        self.token = token if token is not None else _read_token()
        self.timeout = timeout

    def _post(self, path, payload):
        request = urllib.request.Request(
            f"{self.endpoint}/internal/inference/v1/{path}",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                body = json.loads(error.read())
                code = body.get("error", {}).get("code", "DEPENDENCY_UNAVAILABLE")
            except Exception:
                code = "DEPENDENCY_UNAVAILABLE"
            raise AIRequestError(code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise AIRequestError("DEPENDENCY_UNAVAILABLE") from None
        try:
            validate("InferenceResult", value)
        except ValueError:
            raise AIRequestError("SCHEMA_MISMATCH") from None
        return value

    def quality(self, payload):
        return self._post("quality", payload)

    def runs(self, payload):
        return self._post("runs", payload)


def _read_token():
    path = os.environ.get("AI_TOKEN_FILE")
    if not path:
        raise RuntimeError("AI_TOKEN_FILE is required for inference worker")
    try:
        value = open(path, encoding="utf-8").read().strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Cannot read AI_TOKEN_FILE") from exc
    if not value or not value.isascii() or any(char.isspace() for char in value):
        raise RuntimeError("Invalid AI token")
    return value


class InferenceHeartbeat:
    interval = 10

    def __init__(self, execution, lease, cancel):
        self.execution, self.lease, self.cancel = execution, lease, cancel
        self.stop = Event()
        self.lost = Event()
        self.thread = Thread(target=self._loop, name="inference-lease-heartbeat", daemon=True)

    def _loop(self):
        while not self.stop.wait(self.interval):
            try:
                renewed = self.execution.heartbeat(self.lease)
            except Exception:
                renewed = False
            if renewed:
                continue
            self.lost.set()
            try:
                self.cancel()
            except Exception:
                pass
            return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive() or self.lost.is_set():
            raise InferenceLeaseLost() from None
        return False


class InferenceExecution:
    def __init__(self, engine, client=None):
        development_environment()
        self.engine = engine
        # Sweepers only recover leases and must not require an AI token file.
        self.client = client

    def claim(self, message):
        with transaction(self.engine) as connection:
            return inference_execution.claim(connection, message, str(uuid4()))

    def heartbeat(self, lease):
        with transaction(self.engine) as connection:
            return inference_execution.heartbeat(connection, lease)

    def fail(self, lease, code):
        with transaction(self.engine) as connection:
            return inference_execution.fail(connection, lease, code)

    def commit(self, lease, result):
        with transaction(self.engine) as connection:
            return inference_execution.commit_result(connection, lease, result)

    def recover_expired(self, limit=100):
        with transaction(self.engine) as connection:
            candidates = inference_execution.expired_candidates(connection, limit)
        recovered = 0
        for candidate in candidates:
            with transaction(self.engine) as connection:
                recovered += int(inference_execution.recover(connection, candidate))
        return recovered

    def execute(self, message):
        lease = self.claim(message)
        if lease is None:
            return False
        cancelled = Event()
        try:
            with InferenceHeartbeat(self, lease, cancelled.set):
                client = self.client or InferenceHTTPClient()
                quality_payload = request_payload(
                    lease, datetime.now(timezone.utc) + timedelta(seconds=15)
                )
                quality = client.quality(quality_payload)
                validate_result(quality, lease)
                if any(
                    quality[name]
                    for name in ("detections", "crops", "ocr_fields", "entities", "relations")
                ):
                    raise AIRequestError("SCHEMA_MISMATCH")
                if quality["outcome"] == "needs_retake":
                    if not any(item["status"] == "needs_retake" for item in quality["quality"]):
                        raise AIRequestError("SCHEMA_MISMATCH")
                    result = quality
                else:
                    if quality["outcome"] != "facts_ready":
                        raise AIRequestError("SCHEMA_MISMATCH")
                    if any(item["status"] != "pass" for item in quality["quality"]):
                        raise AIRequestError("SCHEMA_MISMATCH")
                    # The runs call uses the same immutable request content and
                    # attempt fencing; only the absolute deadline changes.
                    runs_payload = request_payload(
                        lease, datetime.now(timezone.utc) + timedelta(seconds=200)
                    )
                    result = client.runs(runs_payload)
                    validate_result(result, lease)
                    if result["quality"] != quality["quality"]:
                        raise AIRequestError("SCHEMA_MISMATCH")
                    if result["outcome"] == "needs_retake" and not any(
                        item["status"] == "needs_retake" for item in result["quality"]
                    ):
                        raise AIRequestError("SCHEMA_MISMATCH")
                    if result["outcome"] != "needs_retake" and any(
                        item["status"] == "needs_retake" for item in result["quality"]
                    ):
                        raise AIRequestError("SCHEMA_MISMATCH")
            self.commit(lease, result)
        except InferenceLeaseLost:
            raise
        except AIRequestError as error:
            self.fail(lease, error.code)
            raise
        except InferenceInvalidResult:
            self.fail(lease, "SCHEMA_MISMATCH")
            raise
        except Exception:
            self.fail(lease, "INTERNAL_ERROR")
            raise
        return True
