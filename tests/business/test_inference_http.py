"""Worker uses real socket RPC; synthetic service covers bounded transport and pins."""

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from apps.ai_inference.app.fixture import compute
from apps.ai_inference.app.settings import Settings
from packages.application.inference_execution import AIRequestError, InferenceHTTPClient
from packages.domain.inference_execution import InferenceLeaseLost
from tests.helpers import configured, make_request


@contextmanager
def service(mode="ok", *, actual=None):
    with configured() as token:
        settings = Settings.from_env()
        identity = actual or settings.identity
        source = make_request(settings)
        calls = []
        entered = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_wire(self):
                path = self.path.rsplit("/", 1)[-1]
                calls.append(path)
                if path == "ready":
                    value = {
                        "status": "ready",
                        "model_bundle_id": identity["model_bundle_id"],
                        "active_attempt_id": None,
                    }
                elif path == "version":
                    value = identity
                else:
                    payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    value = asyncio.run(compute(payload, settings, path))
                    value["execution_identity"] = identity
                    entered.set()
                    if mode == "echo":
                        value["fencing_token"] += 1
                    if mode == "result_identity":
                        value["execution_identity"] = dict(identity, service_commit="drifted")
                    if mode == "slow_body":
                        time.sleep(0.5)
                raw = json.dumps(value).encode()
                if mode == "slow_headers" and path in {"quality", "runs"}:
                    try:
                        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                        for _ in range(30):
                            self.wfile.write(b"a")
                            self.wfile.flush()
                            time.sleep(0.03)
                    except OSError:
                        pass
                    return
                if mode == "oversized" and path == "runs":
                    raw = b"x" * (2 * 1024 * 1024 + 1)
                if mode == "malformed" and path == "runs":
                    raw = b"{"
                self.send_response(302 if mode == "redirect" else 200)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

            do_GET = handle_wire
            do_POST = handle_wire

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = InferenceHTTPClient(f"http://127.0.0.1:{server.server_port}", token)
            yield client, source, calls, entered
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def test_fixture_preflight_and_actual_simulated_identity():
    with service() as (client, source, calls, _):
        value = client.quality(source)
        assert value["execution_identity"]["is_simulated"] is True
        assert calls == ["ready", "version", "quality"]


@pytest.mark.parametrize(
    "mode,code",
    [
        ("echo", "SCHEMA_MISMATCH"),
        ("result_identity", "SCHEMA_MISMATCH"),
        ("oversized", "SCHEMA_MISMATCH"),
        ("malformed", "SCHEMA_MISMATCH"),
        ("redirect", "DEPENDENCY_UNAVAILABLE"),
    ],
)
def test_bad_results_and_redirects_fail_closed(mode, code):
    with service(mode) as (client, source, _, _):
        with pytest.raises(AIRequestError) as error:
            client.runs(source)
        assert error.value.code == code


def test_changed_pin_rejects_before_post():
    with service() as (client, source, calls, _):
        source["dictionary_sha256"] = "f" * 64
        with pytest.raises(AIRequestError) as error:
            client.quality(source)
        assert error.value.code == "MODEL_VERSION_UNAVAILABLE"
        assert calls == ["ready", "version"]


def test_cpu_requires_explicit_full_expected_identity():
    with configured():
        actual = dict(
            Settings.from_env().identity,
            is_simulated=False,
            adapter_id="dfine-coco80-rgb-stretch-v1",
            runtime_profile="dfine-cpu-fp32-ocrv6smallcpu-v1",
            detector_device="cpu",
            ocr_device="cpu",
        )
    with service(actual=actual) as (client, source, calls, _):
        with pytest.raises(AIRequestError) as error:
            client.runs(source)
        assert error.value.code == "MODEL_VERSION_UNAVAILABLE"
        assert "runs" not in calls
        client.expected = actual
        assert client.runs(source)["execution_identity"] == actual
    with pytest.raises(ValueError):
        InferenceHTTPClient(token="synthetic", expected_version={})


@pytest.mark.parametrize("mode", ["slow_body", "slow_headers"])
def test_deadline_bounds_entire_http_even_trickling_headers(mode):
    with service(mode) as (client, source, _, _):
        client.timeout = 0.15
        started = time.monotonic()
        with pytest.raises(AIRequestError) as error:
            client.runs(source)
        assert error.value.code == "AI_TIMEOUT"
        assert time.monotonic() - started < 0.5


def test_lease_cancel_shuts_down_active_rpc_socket():
    with service("slow_body") as (client, source, _, entered):
        errors = []

        def call():
            try:
                client.runs(source)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=call)
        thread.start()
        assert entered.wait(2)
        client.cancel()
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert isinstance(errors[0], InferenceLeaseLost)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com",
        "http://127.0.0.1/payload",
        "https://user:secret@example.com",
        "file:///tmp/token",
    ],
)
def test_uncontrolled_endpoint_rejected(endpoint):
    with pytest.raises(RuntimeError):
        InferenceHTTPClient(endpoint, "synthetic")
