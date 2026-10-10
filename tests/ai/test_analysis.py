"""Read boundaries without SDK/business packages; numeric PNG cases in CPU environment."""

import copy
import hashlib
import importlib.util
import io
import multiprocessing
import os
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.analysis import MAX_ANALYSIS_BYTES, AnalysisReader, decode_analysis
from apps.ai_inference.analysis_cpu import run_analysis_pipeline
from apps.ai_inference.inputs import verify_image_inputs
from apps.ai_inference.pipeline_cpu import PipelineOptions
from packages.inference_protocol.contract import validate
from packages.inference_protocol.hashing import request_hash
from tools.ml.analysis_fixture import LAB, TENANT, object_server, payload_for

REAL = all(importlib.util.find_spec(name) for name in ("PIL", "numpy", "cv2"))


class AnalysisReadTests(unittest.TestCase):
    def setUp(self):
        self.raw = b"synthetic bytes: read tests do not decode pixels"
        self.payload = payload_for([self.raw])
        self.ref = self.payload["image_refs"][0]

    def read(self, settings, deadline=None):
        return AnalysisReader(settings).read(
            self.ref, TENANT, LAB, deadline or time.monotonic() + 3
        )

    def test_fixed_get_version_signature_and_no_environment_proxy(self):
        with object_server(self.payload, [self.raw]) as (settings, seen):
            with patch.dict(
                os.environ, HTTP_PROXY="http://invalid:1", HTTPS_PROXY="http://invalid:1"
            ):
                self.assertEqual(self.read(settings), self.raw)
            self.assertEqual(len(seen), 1)
            path, headers = seen[0]
            self.assertTrue(path.endswith("?versionId=synthetic%2Fv0%2Bexact%3D"))
            self.assertIn("AWS4-HMAC-SHA256", headers["authorization"])
            self.assertNotIn(settings.secret_key, repr(settings))

    def test_invalid_refs_and_endpoint_configuration_never_connect(self):
        with object_server(self.payload, [self.raw]) as (settings, seen):
            for key, value in (
                ("object_key", "https://attacker/image.png"),
                ("object_key", self.ref["object_key"].replace("/analysis/", "/original/")),
                ("object_version", "null"),
                ("object_version", ""),
                ("object_version", "line\nfeed"),
                ("sha256", "A" * 64),
                ("image_id", self.ref["image_id"].upper()),
            ):
                changed = {**self.ref, key: value}
                # Generated UUID may contain no letters; use a known noncanonical UUID.
                if key == "image_id":
                    changed[key] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
                with self.assertRaises(AdapterError):
                    AnalysisReader(settings).read(changed, TENANT, LAB, time.monotonic() + 1)
            with self.assertRaises(AdapterError):
                AnalysisReader(replace(settings, allowed_tenants=frozenset({LAB}))).read(
                    self.ref, TENANT, LAB, time.monotonic() + 1
                )
            for endpoint in (
                "http://192.168.1.1",
                "http://evil.test",
                settings.endpoint + "/",
                "https://u:p@host",
                "https://host?x=1",
            ):
                with self.assertRaises(AdapterError):
                    AnalysisReader(replace(settings, endpoint=endpoint))
            self.assertEqual(seen, [])

    def test_response_failures_no_redirect_or_partial_result(self):
        cases = [
            ({"status": 302}, "DEPENDENCY_UNAVAILABLE"),
            ({"status": 404}, "OBJECT_NOT_FOUND"),
            ({"headers": {"x-amz-version-id": "replaced"}}, "HASH_MISMATCH"),
            ({"headers": {"x-amz-version-id": None}}, "SCHEMA_MISMATCH"),
            ({"headers": {"Content-Type": "image/jpeg"}}, "SCHEMA_MISMATCH"),
            ({"headers": {"Content-Length": ["42", "42"]}}, "SCHEMA_MISMATCH"),
            ({"headers": {"Content-Length": "-1"}}, "SCHEMA_MISMATCH"),
            ({"headers": {"Content-Length": str(MAX_ANALYSIS_BYTES + 1)}}, "IMAGE_TOO_LARGE"),
            ({"headers": {"Content-Encoding": "gzip"}}, "SCHEMA_MISMATCH"),
            ({"headers": {"Transfer-Encoding": "chunked"}}, "SCHEMA_MISMATCH"),
            ({"truncated": True}, "IMAGE_INVALID"),
        ]
        for arguments, code in cases:
            with (
                self.subTest(arguments=arguments),
                object_server(self.payload, [self.raw], **arguments) as (settings, seen),
            ):
                with self.assertRaises(AdapterError) as error:
                    self.read(settings)
                self.assertEqual(error.exception.code, code)
                self.assertEqual(len(seen), 1)
        with object_server(self.payload, [b"different content"]) as (settings, _):
            with self.assertRaises(AdapterError) as error:
                self.read(settings)
            self.assertEqual(error.exception.code, "HASH_MISMATCH")

    def test_read_deadline_and_spawn_reclaimed_then_next_request(self):
        before = {child.pid for child in multiprocessing.active_children()}
        with object_server(self.payload, [self.raw], delay=2) as (settings, _):
            with self.assertRaises(AdapterError) as error:
                self.read(settings, time.monotonic() + 0.05)
            self.assertEqual(error.exception.code, "AI_TIMEOUT")
            with patch.dict(os.environ, APP_ENV="test"), self.assertRaises(AdapterError) as error:
                run_analysis_pipeline(
                    PipelineOptions(quality_only=True, timeout_seconds=0.1), self.payload, settings
                )
            self.assertEqual(error.exception.code, "AI_TIMEOUT")
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)
        with object_server(self.payload, [self.raw]) as (settings, _):
            self.assertEqual(self.read(settings), self.raw)

    def test_version_schema_hash_graph_and_host_validation(self):
        for version in (None, "", "null", "x" * 201, "a\tb", "v\n"):
            changed = copy.deepcopy(self.payload)
            changed["image_refs"][0]["object_version"] = version
            changed["request_hash"] = request_hash(changed)
            with self.assertRaises(ValueError):
                validate("InferenceRequest", changed)
            with self.assertRaises(AdapterError):
                verify_image_inputs(changed, frozenset({TENANT}))
        changed = copy.deepcopy(self.payload)
        del changed["image_refs"][0]["object_version"]
        with self.assertRaises(ValueError):
            validate("InferenceRequest", changed)
        changed = copy.deepcopy(self.payload)
        changed["image_refs"][0]["object_version"] = "another"
        self.assertNotEqual(request_hash(changed), self.payload["request_hash"])
        with self.assertRaises(AdapterError) as error:
            verify_image_inputs(changed, frozenset({TENANT}))
        self.assertEqual(error.exception.code, "HASH_MISMATCH")
        changed = copy.deepcopy(self.payload)
        changed["attempt_id"] = LAB
        changed["deadline_at"] = "2026-01-01T00:00:00Z"
        self.assertEqual(request_hash(changed), self.payload["request_hash"])
        for mutation in (
            lambda p: p["image_refs"].append(copy.deepcopy(p["image_refs"][0])),
            lambda p: p["image_refs"][0].update(role="detail", parent_image_id=LAB),
        ):
            changed = copy.deepcopy(self.payload)
            mutation(changed)
            changed["request_hash"] = request_hash(changed)
            with self.assertRaises(AdapterError):
                verify_image_inputs(changed, frozenset({TENANT}))
        with object_server(self.payload, [self.raw]) as (settings, seen):
            with patch.dict(os.environ, APP_ENV="production"), self.assertRaises(AdapterError):
                run_analysis_pipeline(PipelineOptions(quality_only=True), self.payload, settings)
            with patch.dict(os.environ, APP_ENV="test"), self.assertRaises(AdapterError) as error:
                run_analysis_pipeline(PipelineOptions(quality_only=True), changed, settings)
            self.assertEqual(seen, [])

    def test_dripping_http10_body_cannot_extend_total_deadline(self):
        with object_server(self.payload, [self.raw], chunk_delay=0.04) as (settings, _):
            started = time.monotonic()
            with self.assertRaises(AdapterError) as error:
                self.read(settings, started + 0.2)
            self.assertEqual(error.exception.code, "AI_TIMEOUT")
            self.assertLess(time.monotonic() - started, 0.5)


@unittest.skipUnless(REAL, "PNG/quality tests require the real CPU environment")
class AnalysisPixelTests(unittest.TestCase):
    def png(self, mode="RGB", size=(64, 32), **options):
        from PIL import Image

        stream = io.BytesIO()
        with Image.new(mode, size) as image:
            image.save(stream, format="PNG", **options)
        return stream.getvalue()

    def decode(self, raw):
        return decode_analysis(raw, hashlib.sha256(raw).hexdigest())

    def test_normalized_pixels_no_rotation_resize_and_reject_originals(self):
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        with self.decode(self.png()) as image:
            self.assertEqual(image.size, (64, 32))
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.info, {})
        metadata = PngInfo()
        metadata.add_text("comment", "synthetic")
        late = PngInfo()
        late.add(b"tEXt", b"comment\0synthetic", after_idat=True)
        exif = Image.Exif()
        exif[274] = 6
        animated = io.BytesIO()
        with Image.new("RGB", (64, 32)) as first, Image.new("RGB", (64, 32), "white") as second:
            first.save(animated, format="PNG", save_all=True, append_images=[second])
        for raw in (
            self.png("L"),
            self.png("RGBA"),
            self.png(pnginfo=metadata),
            self.png(pnginfo=late),
            self.png(exif=exif),
            animated.getvalue(),
            b"JPEG",
        ):
            with self.assertRaises(AdapterError) as error:
                self.decode(raw)
            self.assertEqual(error.exception.code, "IMAGE_INVALID")
        with self.assertRaises(AdapterError) as error:
            self.decode(self.png(size=(10001, 1)))
        self.assertEqual(error.exception.code, "IMAGE_TOO_LARGE")

    def test_analysis_over_original_12mib_limit_is_supported(self):
        import numpy as np
        from PIL import Image

        stream = io.BytesIO()
        with Image.fromarray(
            np.random.default_rng(36).integers(0, 256, (2200, 2200, 3), dtype=np.uint8)
        ) as image:
            image.save(stream, format="PNG")
        raw = stream.getvalue()
        self.assertGreater(len(raw), 12 * 1024 * 1024)
        with self.decode(raw) as image:
            self.assertEqual(image.size, (2200, 2200))

    def test_remote_pipeline_keeps_worker_ids_and_whole_batch_gate(self):
        raw = self.png()
        payload = payload_for([raw, raw])
        with (
            object_server(payload, [raw, raw]) as (settings, seen),
            patch.dict(os.environ, APP_ENV="test"),
        ):
            report = run_analysis_pipeline(
                PipelineOptions("missing", "missing", "missing"), payload, settings
            )
            self.assertEqual(len(seen), 2)
        self.assertEqual(report["outcome"], "needs_retake")
        self.assertFalse(report["models_executed"])
        self.assertEqual(report["schema_version"], "cpu-pipeline-analysis-v1")
        self.assertEqual(
            [row["image_id"] for row in report["images"]],
            [ref["image_id"] for ref in payload["image_refs"]],
        )
        self.assertEqual(
            [row["object_version"] for row in report["images"]],
            [ref["object_version"] for ref in payload["image_refs"]],
        )
        self.assertEqual(report["artifacts"], {})


if __name__ == "__main__":
    unittest.main()
