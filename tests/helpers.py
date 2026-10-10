"""Synthetic data and temporary secrets only; no external services."""

import base64
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from packages.inference_protocol.hashing import request_hash

TENANT = "11111111-1111-4111-8111-111111111111"


@contextmanager
def configured(scenario="no_targets", delay=0):
    with tempfile.TemporaryDirectory(prefix="labsafe-test-") as directory:
        token = secrets.token_urlsafe(32)
        token_file = Path(directory) / "token"
        token_file.write_text(token, encoding="ascii")
        values = {
            "APP_ENV": "test",
            "AI_MODE": "mock",
            "AI_TOKEN_FILE": str(token_file),
            "AI_ALLOWED_TENANTS": TENANT,
            "AI_MAX_INFLIGHT": "1",
            "AI_FIXTURE_SCENARIO": scenario,
            "AI_FIXTURE_DELAY_MS": str(delay),
            "SERVICE_COMMIT": "synthetic-test",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        with patch.dict(os.environ, values):
            yield token


def make_request(settings, *, seconds=10):
    image_id, lab_id, location_id = (str(uuid4()) for _ in range(3))
    # A synthetic 1x1 PNG, not a laboratory image or a quality-evaluation sample.
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jWZkAAAAASUVORK5CYII="
    )
    image_hash = hashlib.sha256(png).hexdigest()
    payload = {
        "run_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "fencing_token": 1,
        "tenant_id": TENANT,
        "laboratory_id": lab_id,
        "item_id": str(uuid4()),
        "submission_revision": 1,
        "model_bundle_id": settings.identity["model_bundle_id"],
        "model_checksum": settings.model_checksum,
        "dictionary_version_id": settings.identity["dictionary_version_id"],
        "dictionary_sha256": settings.dictionary_sha256,
        "pipeline_version": "vision-v1",
        "device_profile": "cpu",
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
        "image_refs": [
            {
                "image_id": image_id,
                "object_key": f"tenant/{TENANT}/lab/{lab_id}/analysis/{image_id}/{image_hash}.png",
                "object_version": "synthetic-analysis-v1",
                "sha256": image_hash,
                "mime_type": "image/png",
                "role": "overview",
                "location_id": location_id,
                "parent_image_id": None,
            }
        ],
    }
    payload["request_hash"] = request_hash(payload)
    return payload


@contextmanager
def http_process(module, port_variable, probe, *, headers=None):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    environment = {**os.environ, port_variable: str(port), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen(
        [sys.executable, "-B", "-m", module],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"Service exited: {process.communicate()[0]}")
                try:
                    if client.get(probe, headers=headers, timeout=0.5).status_code == 200:
                        yield client
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.05)
            raise AssertionError("HTTP process did not become ready")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
