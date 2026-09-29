import asyncio
import copy
import hashlib
import os
import subprocess
import sys
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from apps.ai_inference.app.main import create_app
from apps.ai_inference.app.settings import Settings
from packages.inference_protocol.contract import DOCUMENT, validate
from packages.inference_protocol.hashing import request_hash
from tests.helpers import configured, http_process, make_request

BASE = "/internal/inference/v1"


class FixtureTests(unittest.TestCase):
    def test_authenticated_health_ready_version_follow_contract(self):
        with configured() as token:
            settings = Settings.from_env()
            app = create_app(settings)
            self.assertEqual(app.openapi(), DOCUMENT)
            with TestClient(app) as client:
                for route, schema in (
                    ("health", "Health"),
                    ("ready", "Health"),
                    ("version", "Version"),
                ):
                    response = client.get(
                        f"{BASE}/{route}", headers={"Authorization": f"Bearer {token}"}
                    )
                    self.assertEqual(response.status_code, 200)
                    validate(schema, response.json())
                identity = client.get(
                    f"{BASE}/version", headers={"Authorization": f"Bearer {token}"}
                ).json()
                self.assertTrue(identity["is_simulated"])
                self.assertEqual(identity["purpose"], "development")
                self.assertEqual(identity["detector_device"], "mock")
                lock = (
                    files("apps.ai_inference").joinpath("fixtures/runtime-lock.json").read_bytes()
                )
                self.assertEqual(identity["runtime_lock_sha256"], hashlib.sha256(lock).hexdigest())

    def test_all_contract_routes_require_token(self):
        with configured():
            with TestClient(create_app()) as client:
                for path, operation in DOCUMENT["paths"].items():
                    for method in operation:
                        for headers in ({}, {"Authorization": "Bearer incorrect"}):
                            response = client.request(method, BASE + path, headers=headers, json={})
                            self.assertEqual(response.status_code, 401, response.text)
                            validate("Error", response.json())
                            self.assertNotIn("incorrect", response.text)
                            self.assertFalse(response.json()["error"]["retryable"])
                self.assertEqual(client.post(f"{BASE}/fixture", json={}).status_code, 404)
                self.assertEqual(client.get("/docs").status_code, 404)

    def test_missing_lifespan_is_not_ready(self):
        with configured() as token:
            # Without context manager, TestClient does not enter ASGI lifespan.
            client = TestClient(create_app())
            try:
                response = client.get(f"{BASE}/ready", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(response.status_code, 503)
                validate("Error", response.json())
            finally:
                client.close()

    def test_quality_and_runs_echo_identity_without_false_safe_result(self):
        with configured() as token:
            settings = Settings.from_env()
            payload = make_request(settings)
            with TestClient(create_app(settings)) as client:
                for stage, expected in (("quality", "facts_ready"), ("runs", "needs_review")):
                    response = client.post(
                        f"{BASE}/{stage}",
                        json=payload,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                    validate("InferenceResult", result)
                    self.assertEqual(result["outcome"], expected)
                    for key in ("run_id", "attempt_id", "fencing_token", "request_hash"):
                        self.assertEqual(result[key], payload[key])
                    self.assertEqual(result["detections"], [])
                    self.assertEqual(
                        result["quality"][0]["image_id"], payload["image_refs"][0]["image_id"]
                    )

    def test_retake_and_model_error_branches(self):
        for scenario, status in (("needs_retake", 200), ("error", 500)):
            with self.subTest(scenario=scenario), configured(scenario) as token:
                settings = Settings.from_env()
                with TestClient(create_app(settings)) as client:
                    for stage in ("quality", "runs"):
                        response = client.post(
                            f"{BASE}/{stage}",
                            json=make_request(settings),
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        self.assertEqual(response.status_code, status)
                        validate("InferenceResult" if status == 200 else "Error", response.json())
                        if status == 200:
                            self.assertEqual(response.json()["outcome"], "needs_retake")
                            self.assertEqual(response.json()["quality"][0]["reasons"], ["blur"])
                        else:
                            self.assertEqual(response.json()["error"]["code"], "MODEL_ERROR")

    def test_schema_errors_do_not_echo_input(self):
        with configured() as token:
            settings = Settings.from_env()
            valid = make_request(settings)
            invalid = [
                ({}, None),
                ({**valid, "unknown": "sensitive-value"}, None),
                ({**valid, "fencing_token": True}, None),
                ({**valid, "run_id": "not-a-uuid"}, None),
                ({**valid, "deadline_at": "not-a-date"}, None),
            ]
            with TestClient(create_app(settings)) as client:
                for payload, _ in invalid:
                    response = client.post(
                        f"{BASE}/runs", json=payload, headers={"Authorization": f"Bearer {token}"}
                    )
                    self.assertEqual(response.status_code, 422)
                    validate("Error", response.json())
                    self.assertNotIn("sensitive-value", response.text)
                response = client.post(
                    f"{BASE}/runs",
                    content="{broken",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                self.assertEqual(response.status_code, 422)
                validate("Error", response.json())

    def test_semantic_validation(self):
        with configured() as token:
            settings = Settings.from_env()
            mutations = [
                ("tenant_id", str(uuid4()), "FORBIDDEN"),
                ("model_bundle_id", str(uuid4()), "MODEL_VERSION_UNAVAILABLE"),
                ("dictionary_version_id", str(uuid4()), "MODEL_VERSION_UNAVAILABLE"),
                ("device_profile", "cuda", "MODEL_VERSION_UNAVAILABLE"),
                ("model_checksum", "0" * 64, "HASH_MISMATCH"),
                ("dictionary_sha256", "0" * 64, "HASH_MISMATCH"),
                ("request_hash", "0" * 64, "HASH_MISMATCH"),
            ]
            with TestClient(create_app(settings)) as client:
                for key, value, expected in mutations:
                    payload = make_request(settings)
                    payload[key] = value
                    response = client.post(
                        f"{BASE}/runs", json=payload, headers={"Authorization": f"Bearer {token}"}
                    )
                    self.assertEqual(response.json()["error"]["code"], expected)
                    validate("Error", response.json())
                for field, value, expected in (
                    ("object_key", "https://example.invalid/remote", "UNAUTHORIZED_REF"),
                    ("parent_image_id", str(uuid4()), "VALIDATION_ERROR"),
                    ("role", "detail", "VALIDATION_ERROR"),
                    ("mime_type", "image/jpeg", "VALIDATION_ERROR"),
                ):
                    payload = make_request(settings)
                    payload["image_refs"][0][field] = value
                    payload["request_hash"] = request_hash(payload)
                    response = client.post(
                        f"{BASE}/runs", json=payload, headers={"Authorization": f"Bearer {token}"}
                    )
                    self.assertEqual(response.json()["error"]["code"], expected)

    def test_timeout_releases_capacity(self):
        with configured(delay=100) as token:
            settings = Settings.from_env()
            with TestClient(create_app(settings)) as client:
                for seconds in (-1, 0.03):
                    response = client.post(
                        f"{BASE}/runs",
                        json=make_request(settings, seconds=seconds),
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    self.assertEqual(response.status_code, 504)
                    self.assertTrue(response.json()["error"]["retryable"])
                response = client.post(
                    f"{BASE}/runs",
                    json=make_request(settings),
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(response.status_code, 200)
                status = client.get(f"{BASE}/health", headers={"Authorization": f"Bearer {token}"})
                self.assertIsNone(status.json()["active_attempt_id"])

    def test_single_inflight_and_duplicate_attempt(self):
        async def check():
            with configured(delay=400) as token:
                settings = Settings.from_env()
                app = create_app(settings)
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app),
                        base_url="http://fixture",
                        headers={"Authorization": f"Bearer {token}"},
                    ) as client:
                        payload = make_request(settings)
                        first = asyncio.create_task(client.post(f"{BASE}/runs", json=payload))
                        try:
                            for _ in range(100):
                                health = await client.get(f"{BASE}/health")
                                if health.json()["active_attempt_id"] == payload["attempt_id"]:
                                    break
                                await asyncio.sleep(0.002)
                            else:
                                self.fail("First attempt did not enter the gate")
                            duplicate = await client.post(f"{BASE}/quality", json=payload)
                            self.assertEqual(duplicate.status_code, 409)
                            self.assertEqual(duplicate.json()["error"]["code"], "RUN_IN_PROGRESS")
                            other = copy.deepcopy(payload)
                            other["attempt_id"] = str(uuid4())
                            busy = await client.post(f"{BASE}/runs", json=other)
                            self.assertEqual(busy.status_code, 429)
                            self.assertEqual(busy.json()["error"]["code"], "AI_BUSY")
                            self.assertEqual((await client.get(f"{BASE}/ready")).status_code, 200)
                            self.assertEqual((await first).status_code, 200)
                        finally:
                            if not first.done():
                                first.cancel()
                                await asyncio.gather(first, return_exceptions=True)

        asyncio.run(check())

    def test_invalid_configuration_is_rejected(self):
        for name, value in (
            ("APP_ENV", "production"),
            ("APP_ENV", ""),
            ("APP_ENV", "staging"),
            ("AI_MODE", "real"),
            ("AI_MODE", "fixture"),
            ("AI_MODE", ""),
            ("AI_ALLOWED_TENANTS", "*"),
            ("AI_ALLOWED_TENANTS", ""),
            ("AI_MAX_INFLIGHT", "2"),
            ("AI_FIXTURE_DELAY_MS", "NaN"),
            ("AI_FIXTURE_DELAY_MS", "-1"),
            ("AI_FIXTURE_DELAY_MS", "10001"),
            ("AI_FIXTURE_SCENARIO", "unknown"),
            ("AI_TOKEN_FILE", ""),
        ):
            with self.subTest(name=name, value=value), configured():
                with patch.dict(os.environ, {name: value}):
                    with self.assertRaises(RuntimeError):
                        create_app()
        with configured():
            Path(os.environ["AI_TOKEN_FILE"]).write_text("too-short", encoding="ascii")
            with self.assertRaises(RuntimeError):
                create_app()

    def test_runtime_drift_is_rejected(self):
        with configured(), patch("apps.ai_inference.app.settings.version", return_value="0.0"):
            with self.assertRaisesRegex(RuntimeError, "dependency mismatch"):
                create_app()

    def test_rfc3339_lowercase_timestamp_and_timeout_scenario(self):
        for scenario in ("no_targets", "timeout"):
            with self.subTest(scenario=scenario), configured(scenario) as token:
                settings = Settings.from_env()
                payload = make_request(settings, seconds=0.2 if scenario == "timeout" else 10)
                payload["deadline_at"] = payload["deadline_at"].replace("+00:00", "z").lower()
                with TestClient(create_app(settings)) as client:
                    response = client.post(
                        f"{BASE}/runs",
                        json=payload,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    self.assertEqual(response.status_code, 504 if scenario == "timeout" else 200)

    def test_standalone_ai_http_process(self):
        with configured() as token:
            settings = Settings.from_env()
            headers = {"Authorization": f"Bearer {token}"}
            with http_process(
                "apps.ai_inference.run", "AI_INFERENCE_PORT", f"{BASE}/ready", headers=headers
            ) as client:
                result = client.post(f"{BASE}/runs", json=make_request(settings), headers=headers)
                self.assertEqual(result.status_code, 200)
                validate("InferenceResult", result.json())

    def test_production_cli_fails(self):
        with configured(), patch.dict(os.environ, {"APP_ENV": "production"}):
            result = subprocess.run(
                [sys.executable, "-B", "-m", "apps.ai_inference.run"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("production is disabled", result.stderr)

    def test_ai_import_does_not_load_business_or_model_dependencies(self):
        code = (
            "import sys; import apps.ai_inference.app.main; "
            "names=('celery','redis','sqlalchemy','torch','paddle','onnxruntime',"
            "'packages.domain','packages.application','packages.persistence','packages.rules'); "
            "assert not any(n in sys.modules for n in names)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr)
