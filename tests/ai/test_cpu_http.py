"""Same HTTP routes and gate with actual spawn IPC, synthetic computation."""

import asyncio
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from apps.ai_inference.app.main import create_app
from apps.ai_inference.app.settings import Settings
from apps.ai_inference.supervisor import CPUSupervisor
from tests.ai.test_supervisor import IDENTITY, fake_child, until
from tests.helpers import configured, make_request

BASE = "/internal/inference/v1"


class CPUHTTPTests(unittest.TestCase):
    def test_hash_pinned_candidates_and_forged_score_rejection(self):
        for scenario in ("chemical", "wrong_chemical"):
            with configured() as token:
                settings, app = self.build(scenario)
                headers = {"Authorization": f"Bearer {token}"}
                with TestClient(app) as client:
                    self.await_ready(client, headers)
                    response = client.post(
                        BASE + "/runs", json=make_request(settings), headers=headers
                    )
                    self.assertEqual(
                        response.status_code, 200 if scenario == "chemical" else 500, response.text
                    )
                    if scenario == "chemical":
                        result = response.json()
                        self.assertEqual(result["entities"][0]["resolution"], "resolved")
                        self.assertEqual(result["outcome"], "needs_review")
                        self.assertIn("extraction_context", result)
                    else:
                        self.assertEqual(response.json()["error"]["code"], "MODEL_ERROR")
                        self.assertFalse(app.state.supervisor.ready)

    def test_formal_http_multiline_fields_and_partial_source_rejection(self):
        for scenario in ("fields", "wrong_fields"):
            with configured() as token:
                settings, app = self.build(scenario)
                headers = {"Authorization": f"Bearer {token}"}
                with TestClient(app) as client:
                    self.await_ready(client, headers)
                    response = client.post(
                        BASE + "/runs", json=make_request(settings), headers=headers
                    )
                    if scenario == "fields":
                        self.assertEqual(response.status_code, 200, response.text)
                        result = response.json()
                        field = result["ocr_fields"][0]
                        self.assertEqual(field["normalized_text"], "Ethanol")
                        self.assertEqual(field["confidence"], 0.3)
                        self.assertEqual(len(field["source_lines"]), 2)
                        self.assertEqual(result["outcome"], "needs_review")
                        self.assertEqual(result["entities"], [])
                    else:
                        self.assertEqual(response.status_code, 500, response.text)
                        self.assertEqual(response.json()["error"]["code"], "MODEL_ERROR")
                        self.assertFalse(app.state.supervisor.ready)

    def test_formal_http_link_and_bad_geometry_response(self):
        for scenario in ("association", "wrong_association"):
            with configured() as token:
                settings, app = self.build(scenario)
                headers = {"Authorization": f"Bearer {token}"}
                with TestClient(app) as client:
                    self.await_ready(client, headers)
                    response = client.post(
                        BASE + "/runs", json=make_request(settings), headers=headers
                    )
                    if scenario == "association":
                        self.assertEqual(response.status_code, 200, response.text)
                        result = response.json()
                        self.assertEqual(
                            result["text_regions"][0]["detection_id"],
                            result["detections"][0]["detection_id"],
                        )
                        self.assertEqual(
                            result["crops"][0]["detection_id"],
                            result["text_regions"][0]["detection_id"],
                        )
                        self.assertEqual(result["outcome"], "needs_review")
                    else:
                        self.assertEqual(response.status_code, 500, response.text)
                        self.assertEqual(response.json()["error"]["code"], "MODEL_ERROR")
                        self.assertFalse(app.state.supervisor.ready)

    def build(self, scenario="ok"):
        identity = IDENTITY
        if "chemical" in scenario:
            from tests.ai.test_supervisor import payload, synthetic_result
            from tests.evidence_helpers import add_chemical_context, synthetic_chemical_entries

            identity = add_chemical_context(
                synthetic_result(payload(), IDENTITY), synthetic_chemical_entries()
            )["execution_identity"]
        settings = replace(
            Settings.from_env(),
            mode="cpu",
            identity=identity,
            model_checksum=identity["model_checksum"],
            dictionary_sha256=identity["dictionary_sha256"],
            bundle=SimpleNamespace(identity=identity),
            analysis=scenario,
        )
        with patch(
            "apps.ai_inference.supervisor.CPUSupervisor",
            side_effect=lambda b, a: CPUSupervisor(b, a, target=fake_child),
        ):
            app = create_app(settings)
        return settings, app

    def await_ready(self, client, headers):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            if client.get(BASE + "/ready", headers=headers).status_code == 200:
                return
            time.sleep(0.01)
        self.fail("Synthetic CPU HTTP not ready")

    def test_actual_version_health_result_and_wrong_pin(self):
        with configured() as token:
            settings, app = self.build()
            headers = {"Authorization": f"Bearer {token}"}
            with TestClient(app) as client:
                self.assertEqual(client.get(BASE + "/health", headers=headers).status_code, 200)
                self.await_ready(client, headers)
                self.assertEqual(client.get(BASE + "/version", headers=headers).json(), IDENTITY)
                self.assertEqual(client.get(BASE + "/version").status_code, 401)
                source = make_request(settings)
                result = client.post(BASE + "/runs", json=source, headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertFalse(result.json()["execution_identity"]["is_simulated"])
                source["model_checksum"] = "f" * 64
                self.assertEqual(
                    client.post(BASE + "/runs", json=source, headers=headers).status_code, 422
                )
                oversized = client.post(BASE + "/runs", content=b" " * 65537, headers=headers)
                self.assertEqual(oversized.status_code, 422)
            self.assertIsNone(app.state.supervisor.process)
            self.assertIsNone(app.state.supervisor.receiver)

    def test_busy_same_attempt_conflict_health_and_timeout_dispose(self):
        with configured() as token:
            settings, app = self.build("timeout")
            headers = {"Authorization": f"Bearer {token}"}
            with TestClient(app) as client:
                self.await_ready(client, headers)
                source = make_request(settings, seconds=1)
                results = []
                thread = threading.Thread(
                    target=lambda: results.append(
                        client.post(BASE + "/runs", json=source, headers=headers)
                    )
                )
                thread.start()
                end = time.monotonic() + 1
                while not app.state.supervisor.busy and time.monotonic() < end:
                    time.sleep(0.01)
                self.assertEqual(client.get(BASE + "/ready", headers=headers).status_code, 200)
                conflict = client.post(BASE + "/runs", json=source, headers=headers)
                self.assertEqual(conflict.status_code, 409)
                from uuid import uuid4

                other = dict(source, attempt_id=str(uuid4()))
                busy = client.post(BASE + "/runs", json=other, headers=headers)
                self.assertEqual(busy.status_code, 429)
                thread.join(timeout=4)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0].json()["error"]["code"], "AI_TIMEOUT")
                self.assertEqual(client.get(BASE + "/version", headers=headers).status_code, 503)

    def test_asgi_cancel_reaps_child(self):
        async def exercise(settings, app, token):
            async with app.router.lifespan_context(app):
                await until(lambda: app.state.supervisor.ready)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    task = asyncio.create_task(
                        client.post(
                            BASE + "/runs",
                            json=make_request(settings),
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    )
                    await until(lambda: app.state.supervisor.busy)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIsNone(app.state.supervisor.process)

        with configured() as token:
            settings, app = self.build("timeout")
            asyncio.run(exercise(settings, app, token))


if __name__ == "__main__":
    unittest.main()
