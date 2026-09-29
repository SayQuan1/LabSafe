import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from celery import Celery
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.main import create_app
from apps.worker.app.main import create_celery_app, fixture_echo
from tests.helpers import configured, http_process


class BootstrapTests(unittest.TestCase):
    def test_actual_api_and_development_readiness(self):
        with configured():
            app = create_app()
            self.assertIsInstance(app, FastAPI)
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").json()["status"], "alive")
                result = client.get("/ready")
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["environment"], "test")
                self.assertIn("process only", result.json()["scope"])

    def test_actual_celery_and_registered_smoke_task(self):
        with configured():
            app = create_celery_app()
            self.assertIsInstance(app, Celery)
            self.assertIn("labsafe.fixture.echo", app.tasks)
            self.assertTrue(app.conf.task_acks_late)
            self.assertEqual(app.conf.worker_prefetch_multiplier, 1)
            self.assertEqual(
                fixture_echo({"task": "smoke"}), {"fixture": True, "payload": {"task": "smoke"}}
            )

    def test_invalid_environments_are_rejected(self):
        for environment in ("production", "staging", ""):
            with self.subTest(environment=environment), configured():
                with patch.dict(os.environ, {"APP_ENV": environment}):
                    for factory in (create_app, create_celery_app):
                        with self.assertRaises(RuntimeError):
                            factory()
        with configured():
            os.environ.pop("APP_ENV")
            with self.assertRaises(RuntimeError):
                create_app()

    def test_missing_frameworks_do_not_fall_back(self):
        for module in ("apps.api.app.main", "apps.worker.app.main", "apps.ai_inference.app.main"):
            with self.subTest(module=module):
                code = f"import sys; sys.path.insert(0, {os.getcwd()!r}); import {module}"
                result = subprocess.run(
                    [sys.executable, "-I", "-S", "-B", "-c", code],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ModuleNotFoundError", result.stderr)

    def test_api_starts_as_an_independent_http_process(self):
        with configured(), http_process("apps.api.run", "API_PORT", "/health") as client:
            self.assertEqual(client.get("/ready").status_code, 200)

    def test_worker_configuration_command(self):
        with configured():
            result = subprocess.run(
                [sys.executable, "-B", "-m", "apps.worker.run", "--check"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("broker connectivity not tested", result.stdout)
