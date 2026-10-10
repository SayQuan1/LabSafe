"""Worker-side inference orchestration with bounded, injectable AI RPC."""

import http.client
import json
import os
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from threading import Event, Thread, Timer
from urllib.parse import urlsplit
from uuid import uuid4

from packages.application.dispatch import transaction
from packages.domain.inference_execution import (
    InferenceInvalidResult,
    InferenceLeaseLost,
    request_payload,
    validate_result,
)
from packages.domain.security import ServiceError
from packages.inference_protocol.contract import validate
from packages.inference_protocol.evidence import validate_closure
from packages.persistence import inference_execution
from packages.shared.environment import development_environment


class AIRequestError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class InferenceHTTPClient:
    """Small stdlib client; tests may inject an object with quality/runs methods."""

    def __init__(self, endpoint=None, token=None, timeout=200, expected_version=None):
        self.endpoint = (
            endpoint or os.environ.get("AI_INFERENCE_URL", "http://127.0.0.1:8001")
        ).rstrip("/")
        self.token = token if token is not None else _read_token()
        self.timeout = timeout
        self.connection = None
        self.response_socket = None
        self.cancelled = Event()
        self.expected = expected_version
        if self.expected is None and os.getenv("AI_EXPECTED_VERSION_FILE"):
            try:
                with Path(os.environ["AI_EXPECTED_VERSION_FILE"]).open("rb") as source:
                    raw = source.read(65537)
                if len(raw) > 65536:
                    raise ValueError
                self.expected = json.loads(raw)
                validate("Version", self.expected)
            except (OSError, ValueError):
                raise RuntimeError("Invalid AI expected identity file") from None
        if self.expected is not None:
            validate("Version", self.expected)
            self.expected = json.loads(json.dumps(self.expected, allow_nan=False))
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or any(c.isspace() for c in self.endpoint)
            or "\\" in self.endpoint
        ):
            raise RuntimeError("Invalid controlled AI endpoint")
        # Plaintext is only for loopback development. No proxy/redirect/provider chain.
        import ipaddress

        if parsed.scheme == "http" and parsed.hostname != "localhost":
            try:
                if not ipaddress.ip_address(parsed.hostname).is_loopback:
                    raise ValueError
            except ValueError:
                raise RuntimeError("AI HTTP endpoint must be loopback") from None
        self.parsed = parsed

    def cancel(self):
        self.cancelled.set()
        self._abort_socket()

    def _abort_socket(self):
        connection = self.connection
        if connection is not None:
            try:
                sock = self.response_socket or connection.sock
                if sock:
                    sock.shutdown(socket.SHUT_RDWR)
                connection.close()
            except OSError:
                pass

    def _remaining(self, deadline):
        if self.cancelled.is_set():
            raise InferenceLeaseLost()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AIRequestError("AI_TIMEOUT")
        return remaining

    def _exchange(self, path, deadline, payload=None):
        parsed = self.parsed
        timeout = self._remaining(deadline)
        connection = (
            http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=timeout, context=ssl.create_default_context()
            )
            if parsed.scheme == "https"
            else http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        )
        self.connection = connection
        timer = Timer(timeout, self._abort_socket)
        timer.daemon = True
        timer.start()
        try:
            connection.timeout = min(2, timeout)
            connection.connect()
            connection.sock.settimeout(self._remaining(deadline))
            raw = (
                None
                if payload is None
                else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
            )
            if raw is not None and len(raw) > 64 * 1024:
                raise AIRequestError("VALIDATION_ERROR")
            connection.request(
                "GET" if raw is None else "POST",
                f"/internal/inference/v1/{path}",
                body=raw,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
            if connection.sock:
                connection.sock.settimeout(self._remaining(deadline))
            response = connection.getresponse()
            if response.fp:
                self.response_socket = response.fp.raw._sock
            chunks, size = [], 0
            while True:
                remaining = self._remaining(deadline)
                # HTTPResponse may own the socket after Connection: close.
                if response.fp:
                    response.fp.raw._sock.settimeout(remaining)
                chunk = response.read1(min(65536, 2 * 1024 * 1024 + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    raise AIRequestError("SCHEMA_MISMATCH")
                chunks.append(chunk)
            self._remaining(deadline)
            value = json.loads(b"".join(chunks))
            if response.status != 200:
                try:
                    validate("Error", value)
                    code = value["error"]["code"]
                except (ValueError, KeyError, TypeError):
                    code = "DEPENDENCY_UNAVAILABLE"
                raise AIRequestError(code)
            return value
        except (TimeoutError, socket.timeout):
            raise AIRequestError("AI_TIMEOUT") from None
        except (OSError, http.client.HTTPException):
            if self.cancelled.is_set():
                raise InferenceLeaseLost() from None
            if time.monotonic() >= deadline:
                raise AIRequestError("AI_TIMEOUT") from None
            raise AIRequestError("DEPENDENCY_UNAVAILABLE") from None
        except (ValueError, TypeError):
            raise AIRequestError("SCHEMA_MISMATCH") from None
        finally:
            timer.cancel()
            timer.join(timeout=2)
            connection.close()
            self.connection = None
            self.response_socket = None

    def _preflight(self, payload, deadline):
        ready = self._exchange("ready", deadline)
        actual = self._exchange("version", deadline)
        try:
            validate("Health", ready)
            validate("Version", actual)
            if actual["purpose"] != "development" or actual["device_profile"] != "cpu":
                raise ValueError
            if ready["status"] != "ready" or ready["model_bundle_id"] != actual["model_bundle_id"]:
                raise ValueError
            expected = self.expected
            if expected is None:
                # Only the checked-in fixture may run without an explicit CPU identity pin.
                import hashlib

                fixture = files("apps.ai_inference").joinpath("fixtures")
                model = fixture.joinpath("model.json").read_bytes()
                dictionary = fixture.joinpath("dictionary.json").read_bytes()
                expected = {
                    "purpose": "development",
                    "is_simulated": True,
                    "adapter_id": "fixture-v1",
                    "runtime_profile": "fixture-v1",
                    "detector_device": "mock",
                    "ocr_device": "mock",
                    "model_bundle_id": json.loads(model)["model_bundle_id"],
                    "dictionary_version_id": json.loads(dictionary)["dictionary_version_id"],
                    "model_checksum": hashlib.sha256(model).hexdigest(),
                    "dictionary_sha256": hashlib.sha256(dictionary).hexdigest(),
                    "runtime_lock_sha256": hashlib.sha256(
                        fixture.joinpath("runtime-lock.json").read_bytes()
                    ).hexdigest(),
                    "pipeline_version": "vision-v1",
                    "device_profile": "cpu",
                }
            if any(actual[key] != value for key, value in expected.items()):
                raise ValueError
            if any(
                actual[key] != payload[key]
                for key in (
                    "model_bundle_id",
                    "model_checksum",
                    "dictionary_version_id",
                    "dictionary_sha256",
                    "pipeline_version",
                    "device_profile",
                )
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise AIRequestError("MODEL_VERSION_UNAVAILABLE") from None
        self._remaining(deadline)
        return actual

    def _post(self, path, payload):
        remaining = (
            datetime.fromisoformat(payload["deadline_at"].replace("Z", "+00:00"))
            - datetime.now(timezone.utc)
        ).total_seconds()
        deadline = time.monotonic() + min(remaining, self.timeout, 15 if path == "quality" else 200)
        actual = self._preflight(payload, deadline)
        value = self._exchange(path, deadline, payload)
        try:
            validate("InferenceResult", value)
            validate_closure(value, [r["image_id"] for r in payload["image_refs"]])
            if (
                value.get("execution_identity", actual if actual["is_simulated"] else None)
                != actual
            ):
                raise ValueError
            if any(
                value[key] != payload[key]
                for key in (
                    "run_id",
                    "attempt_id",
                    "fencing_token",
                    "tenant_id",
                    "request_hash",
                    "model_bundle_id",
                    "model_checksum",
                    "dictionary_version_id",
                    "dictionary_sha256",
                    "pipeline_version",
                )
            ) or value["input_hashes"] != [
                {"image_id": row["image_id"], "sha256": row["sha256"]}
                for row in payload["image_refs"]
            ]:
                raise ValueError
        except ValueError:
            raise AIRequestError("SCHEMA_MISMATCH") from None
        value["execution_identity"] = actual
        self._remaining(deadline)
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
    def __init__(self, engine, client=None, prepare=None):
        development_environment()
        self.engine = engine
        # Sweepers only recover leases and must not require an AI token file.
        self.client = client
        self.prepare = prepare

    def claim(self, message):
        with transaction(self.engine) as connection:
            return inference_execution.claim(connection, message, str(uuid4()))

    def heartbeat(self, lease):
        with transaction(self.engine) as connection:
            return inference_execution.heartbeat(connection, lease)

    def fail(self, lease, code):
        with transaction(self.engine) as connection:
            return inference_execution.fail(connection, lease, code)

    def commit(self, lease, result, derivatives=()):
        with transaction(self.engine) as connection:
            return inference_execution.commit_result(connection, lease, result, derivatives)

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
            client = self.client or InferenceHTTPClient()

            def cancel():
                cancelled.set()
                if hasattr(client, "cancel"):
                    client.cancel()

            with InferenceHeartbeat(self, lease, cancel):
                quality_payload = request_payload(
                    lease, datetime.now(timezone.utc) + timedelta(seconds=15)
                )
                quality = client.quality(quality_payload)
                validate_result(quality, lease)
                if any(
                    quality[name]
                    for name in (
                        "detections",
                        "text_regions",
                        "crops",
                        "ocr_fields",
                        "entities",
                        "relations",
                    )
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
                derivatives = ()
                if result["crops"]:
                    if self.prepare is None:
                        raise AIRequestError("DEPENDENCY_UNAVAILABLE")
                    derivatives = self.prepare(lease, result, cancelled)
                if cancelled.is_set():
                    raise InferenceLeaseLost()
            self.commit(lease, result, derivatives)
        except InferenceLeaseLost:
            raise
        except AIRequestError as error:
            self.fail(lease, error.code)
            raise
        except InferenceInvalidResult:
            self.fail(lease, "SCHEMA_MISMATCH")
            raise
        except ServiceError as error:
            self.fail(lease, error.code)
            raise
        except Exception:
            self.fail(lease, "INTERNAL_ERROR")
            raise
        return True
