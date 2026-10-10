"""Loopback object-store simulator with synthetic credentials; engineering tests only."""

import hashlib
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from uuid import uuid4

from apps.ai_inference.analysis import BUCKET, AnalysisSettings
from packages.inference_protocol.hashing import request_hash

TENANT = "11111111-1111-4111-8111-111111111111"
LAB = "22222222-2222-4222-8222-222222222222"


def payload_for(raws, *, seconds=180):
    overview, location = str(uuid4()), str(uuid4())
    refs = []
    for index, raw in enumerate(raws):
        image = str(uuid4()) if index else overview
        sha = hashlib.sha256(raw).hexdigest()
        refs.append(
            {
                "image_id": image,
                "sha256": sha,
                "object_key": f"tenant/{TENANT}/lab/{LAB}/analysis/{image}/{sha}.png",
                "object_version": f"synthetic/v{index}+exact=",
                "mime_type": "image/png",
                "role": "detail" if index else "overview",
                "parent_image_id": overview if index else None,
                "location_id": location,
            }
        )
    value = {
        "run_id": str(uuid4()),
        "attempt_id": str(uuid4()),
        "fencing_token": 1,
        "tenant_id": TENANT,
        "laboratory_id": LAB,
        "item_id": str(uuid4()),
        "submission_revision": 1,
        "model_bundle_id": str(uuid4()),
        "model_checksum": "a" * 64,
        "dictionary_version_id": str(uuid4()),
        "dictionary_sha256": "b" * 64,
        "pipeline_version": "vision-v1",
        "device_profile": "cpu",
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
        "image_refs": refs,
    }
    value["request_hash"] = request_hash(value)
    return value


@contextmanager
def object_server(
    payload, raws, *, status=200, headers=None, delay=0, truncated=False, chunk_delay=0
):
    objects = {
        f"/{BUCKET}/{ref['object_key']}?versionId={quote(ref['object_version'], safe='')}": (
            ref,
            raw,
        )
        for ref, raw in zip(payload["image_refs"], raws)
    }
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            seen.append((self.path, dict(self.headers)))
            if self.path not in objects:
                self.send_error(404)
                return
            ref, raw = objects[self.path]
            time.sleep(delay)
            self.send_response(status)
            response_headers = {
                "Content-Length": str(len(raw)),
                "Content-Type": "image/png",
                "x-amz-version-id": ref["object_version"],
                **(headers or {}),
            }
            for name, value in response_headers.items():
                if value is not None:
                    for entry in value if isinstance(value, list) else [value]:
                        self.send_header(name, entry)
            self.end_headers()
            try:
                content = raw[: len(raw) // 2] if truncated else raw
                if chunk_delay:
                    for byte in content:
                        time.sleep(chunk_delay)
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                else:
                    self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    endpoint = f"http://127.0.0.1:{server.server_port}"
    settings = AnalysisSettings(
        endpoint, (endpoint,), frozenset({TENANT}), "synthetic", "test-only"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield settings, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
