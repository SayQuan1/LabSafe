"""Versioned loopback S3 simulator for engineering tests, not IAM/TLS acceptance."""

import hashlib
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from apps.ai_inference.analysis import AnalysisSettings


@contextmanager
def evidence_store(source, raws, *, fail_put_at=None, wrong_source_version=False, delay_get=0):
    import time

    objects = {
        ref["object_key"]: (ref["object_version"], raw)
        for ref, raw in zip(source["image_refs"], raws)
    }
    uploads, seen = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def parts(self):
            parsed = urlsplit(self.path)
            return unquote(parsed.path.removeprefix("/labsafe-private/")), parse_qs(parsed.query)

        def error(self, status, code):
            raw = f"<Error><Code>{code}</Code></Error>".encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)

        def serve_object(self):
            key, query = self.parts()
            seen.append((self.command, key, query))
            if key not in objects:
                self.error(404, "NoSuchKey")
                return
            version, raw = objects[key]
            if self.command == "GET" and query.get("versionId") != [version]:
                self.error(404, "NoSuchVersion")
                return
            if self.command == "GET":
                time.sleep(delay_get)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("x-amz-version-id", "wrong" if wrong_source_version else version)
            self.end_headers()
            if self.command == "GET":
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        do_GET = serve_object
        do_HEAD = serve_object

        def do_PUT(self):
            key, query = self.parts()
            seen.append(("PUT", key, query))
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if len(uploads) == fail_put_at:
                self.error(403, "AccessDenied")
                return
            version = f"synthetic-crop/v{len(uploads)}+exact="
            objects[key] = (version, raw)
            uploads.append(
                {
                    "key": key,
                    "version": version,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            )
            self.send_response(200)
            self.send_header("x-amz-version-id", version)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    endpoint = f"http://127.0.0.1:{server.server_port}"
    settings = AnalysisSettings(
        endpoint, (endpoint,), frozenset({source["tenant_id"]}), "synthetic", "test-only"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield settings, uploads, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
