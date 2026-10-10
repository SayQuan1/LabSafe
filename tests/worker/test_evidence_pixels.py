"""Real pixels plus SDK Stubber, in the independent evidence environment."""

import copy
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from threading import Event, Timer
from unittest.mock import patch

import numpy as np
from botocore.response import StreamingBody
from botocore.stub import Stubber

from packages.application.inference_evidence import prepare_evidence
from packages.domain.inference_evidence import derivative_key, validate_derivatives
from packages.domain.inference_execution import request_payload
from packages.domain.security import ServiceError
from packages.image_evidence.perspective import (
    CropError,
    CropTransform,
    encode_rgb_png,
    rebuild_crop,
    recognition_rgb,
    rgb_digest,
)
from packages.inference_protocol.fields import extract_fields
from packages.storage.s3 import BUCKET, S3Settings, S3Storage
from tests.evidence_helpers import (
    add_bottles,
    add_chemical_context,
    add_regions,
    lease,
    result_for,
    synthetic_chemical_entries,
    with_source_hash,
)


def pixel_case():
    y, x = np.indices((30, 40))
    rgb = np.stack((x * 5, y * 7, x + y), axis=-1).astype(np.uint8)
    raw = encode_rgb_png(rgb)
    value = with_source_hash(lease(), hashlib.sha256(raw).hexdigest())
    result = add_regions(value, result_for(value, "needs_review"))
    for index, crop in enumerate(result["crops"]):
        recipe = CropTransform(((0, 0), (1, 0), (1, 1), (0, 1)), 40 if index == 0 else 10, 30)
        crop.update(recipe.as_dict())
        evidence = rebuild_crop(rgb, recipe)
        rotation = 90 if index else 0
        oriented = recognition_rgb(evidence.pixels, rotation)
        crop["evidence"] = {
            "png_sha256": evidence.sha256,
            "png_size_bytes": len(evidence.png),
            "source_rgb_sha256": rgb_digest(rgb),
            "recognition_rotation_ccw": rotation,
            "recognition_width": oriented.shape[1],
            "recognition_height": oriented.shape[0],
            "recognition_rgb_sha256": rgb_digest(oriented),
        }
    source = request_payload(value, datetime.now(timezone.utc) + timedelta(seconds=20))
    return value, source, result, raw


class MemoryStorage:
    def __init__(self, source, raw, fail_at=None):
        self.source, self.raw, self.fail_at = source, raw, fail_at
        self.reads, self.uploads = [], []

    def read_analysis(self, tenant, laboratory, reference):
        assert tenant == self.source["tenant_id"] and laboratory == self.source["laboratory_id"]
        assert reference == self.source["image_refs"][0]
        self.reads.append(reference)
        return self.raw

    def put_derivative(self, key, raw):
        if len(self.uploads) == self.fail_at:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Injected storage failure")
        self.uploads.append((key, raw))
        return f"exact-crop/v{len(self.uploads)}+="


class EvidencePixelsTests(unittest.TestCase):
    def test_pinned_candidates_rebuilt_with_real_pixels_and_forgery_before_storage(self):
        value, source, result, raw = pixel_case()
        add_bottles(value, result)
        result["text_regions"][0]["raw_text"] = "NAME:Alpha"
        result["text_regions"][1]["raw_text"] = "NAME:甲"
        add_chemical_context(result, synthetic_chemical_entries())
        for key in ("model_checksum", "dictionary_sha256"):
            source[key] = result[key]
        from packages.inference_protocol.hashing import request_hash

        source["request_hash"] = request_hash(source)
        result["request_hash"] = source["request_hash"]
        store = MemoryStorage(source, raw)
        artifacts = prepare_evidence(store, source, result)
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(result["entities"][0]["resolution"], "resolved")
        changed = copy.deepcopy(result)
        changed["entities"][0]["candidates"][0]["confidence"] = 0.1
        store = MemoryStorage(source, raw)
        with self.assertRaises(ValueError):
            prepare_evidence(store, source, changed)
        self.assertEqual(store.reads, [])
        self.assertEqual(store.uploads, [])

    def test_multiline_field_both_pixel_crops_verified_and_forgery_before_storage(self):
        value, source, result, raw = pixel_case()
        add_bottles(value, result)
        result["text_regions"][0].update(raw_text="NAME", confidence=0.9)
        result["text_regions"][1].update(raw_text="Ethanol", confidence=0.3)
        result["ocr_fields"] = extract_fields(result["text_regions"])
        store = MemoryStorage(source, raw)
        artifacts = prepare_evidence(store, source, result)
        validate_derivatives(value, result, artifacts)
        self.assertEqual(len(artifacts), 2)
        self.assertEqual(
            {a.crop_id for a in artifacts},
            {s["crop_id"] for s in result["ocr_fields"][0]["source_lines"]},
        )
        self.assertEqual(len(store.reads), 1)
        changed = copy.deepcopy(result)
        changed["ocr_fields"][0]["source_lines"].pop()
        store = MemoryStorage(source, raw)
        with self.assertRaises(ValueError):
            prepare_evidence(store, source, changed)
        self.assertEqual(store.reads, [])
        self.assertEqual(store.uploads, [])

    def test_real_evidence_child_timeout_and_cancellation_kill_and_join(self):
        from apps.worker.inference_evidence import BoundedEvidencePrepare
        from packages.domain.inference_execution import InferenceLeaseLost
        from tools.ml.evidence_fixture import evidence_store
        from tools.ml.smoke_worker_evidence import worker_environment

        value, source, result, raw = pixel_case()
        original_start = subprocess.Popen
        for mode in ("timeout", "cancel"):
            children = []

            def start(*args, **kwargs):
                child = original_start(*args, **kwargs)
                children.append(child)
                return child

            with tempfile.TemporaryDirectory() as directory:
                with evidence_store(source, [raw], delay_get=4) as (settings, uploads, _):
                    cancel = Event()
                    timer = Timer(0.15, cancel.set) if mode == "cancel" else None
                    environment = worker_environment(directory, settings.endpoint, sys.executable)
                    with (
                        patch.dict(os.environ, environment),
                        patch("apps.worker.inference_evidence.subprocess.Popen", start),
                        patch("apps.worker.inference_evidence.EVIDENCE_SECONDS", 1.5),
                    ):
                        try:
                            if timer:
                                timer.start()
                            with self.assertRaises(
                                InferenceLeaseLost if timer else ServiceError
                            ) as error:
                                BoundedEvidencePrepare()(value, result, cancel)
                            if not timer:
                                self.assertEqual(error.exception.code, "STAGE_TIMEOUT")
                        finally:
                            if timer:
                                timer.cancel()
                                timer.join(timeout=2)
                    self.assertEqual(uploads, [])
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].poll())

    def test_two_crops_one_source_and_recognition_rotation_verified(self):
        value, source, result, raw = pixel_case()
        store = MemoryStorage(source, raw)
        artifacts = prepare_evidence(store, source, result)
        validate_derivatives(value, result, artifacts)
        self.assertEqual(len(store.reads), 1)
        self.assertEqual(len(artifacts), 2)
        for artifact, (key, png) in zip(artifacts, store.uploads):
            self.assertEqual(artifact.object_key, key)
            self.assertEqual(artifact.sha256, hashlib.sha256(png).hexdigest())
            self.assertEqual(artifact.size_bytes, len(png))
        self.assertEqual(result["crops"][1]["evidence"]["recognition_rotation_ccw"], 90)
        self.assertFalse(
            set(sys.modules) & {"sqlalchemy", "onnxruntime", "celery", "redis", "paddle"}
        )

    def test_linked_and_ambiguous_text_preserve_actual_crop_bytes(self):
        for count in (1, 2):
            value, source, result, raw = pixel_case()
            baseline = prepare_evidence(MemoryStorage(source, raw), source, result)
            add_bottles(value, result, count)
            store = MemoryStorage(source, raw)
            artifacts = prepare_evidence(store, source, result)
            validate_derivatives(value, result, artifacts)
            self.assertEqual([r.sha256 for r in baseline], [r.sha256 for r in artifacts])
            self.assertEqual([r.crop_id for r in baseline], [r.crop_id for r in artifacts])
            for artifact in artifacts:
                self.assertEqual(
                    artifact.detection_id,
                    result["detections"][0]["detection_id"] if count == 1 else None,
                )

    def test_wrong_link_is_rejected_before_storage_reads(self):
        value, source, result, raw = pixel_case()
        add_bottles(value, result, 1)
        result["detections"][0]["bbox"] = [0, 0, 0.7, 1]
        store = MemoryStorage(source, raw)
        with self.assertRaises(ValueError):
            prepare_evidence(store, source, result)
        self.assertEqual(store.reads, [])
        self.assertEqual(store.uploads, [])

    def test_second_crop_failure_returns_no_partial_result(self):
        _, source, result, raw = pixel_case()
        store = MemoryStorage(source, raw, fail_at=1)
        with self.assertRaises(ServiceError):
            prepare_evidence(store, source, result)
        self.assertEqual(len(store.uploads), 1)  # Unreferenced orphan, no DB handle available.

    def test_corrupt_png_source_recognition_geometry_and_size_rejected(self):
        for field in (
            "source_rgb_sha256",
            "png_sha256",
            "png_size_bytes",
            "recognition_rgb_sha256",
        ):
            _, source, result, raw = pixel_case()
            crop = result["crops"][0]
            crop["evidence"][field] = 1 if field == "png_size_bytes" else "a" * 64
            store = MemoryStorage(source, raw)
            with self.subTest(field=field), self.assertRaises(CropError):
                prepare_evidence(store, source, result)
            self.assertEqual(store.uploads, [])
        _, source, result, raw = pixel_case()
        with self.assertRaises(CropError):
            prepare_evidence(MemoryStorage(source, raw[:-1]), source, result)
        changed = copy.deepcopy(result)
        changed["crops"][0]["quad"] = [{"x": 0, "y": 0}] * 4
        changed["text_regions"][0]["quad"] = changed["crops"][0]["quad"]
        with self.assertRaises(ValueError):
            prepare_evidence(MemoryStorage(source, raw), source, changed)

    def test_real_sdk_pinned_get_put_and_verified_exact_version_reuse(self):
        value, source, result, raw = pixel_case()
        storage = S3Storage(
            S3Settings(
                internal_endpoint="http://127.0.0.1:9000",
                public_endpoint="https://labsafe.test",
                public_origin="https://labsafe.test",
                access_key="synthetic",
                secret_key="test-only",
            )
        )
        try:
            stub = Stubber(storage.internal)
            body = StreamingBody(io.BytesIO(raw), len(raw))
            reference = source["image_refs"][0]
            stub.add_response(
                "get_object",
                {
                    "Body": body,
                    "ContentLength": len(raw),
                    "ContentType": "image/png",
                    "VersionId": reference["object_version"],
                },
                {
                    "Bucket": BUCKET,
                    "Key": reference["object_key"],
                    "VersionId": reference["object_version"],
                },
            )
            for index, crop in enumerate(result["crops"]):
                key = derivative_key(
                    value.tenant_id,
                    value.input.laboratory_id,
                    value.input.run_id,
                    crop["crop_id"],
                    crop["evidence"]["png_sha256"],
                )
                png = MemoryStorage(source, raw)
                prepare_evidence(png, source, result)
                png = png.uploads[index][1]
                stub.add_client_error(
                    "head_object",
                    service_error_code="NoSuchKey",
                    http_status_code=404,
                    expected_params={"Bucket": BUCKET, "Key": key},
                )
                stub.add_response(
                    "put_object",
                    {"VersionId": f"crop/v{index}+="},
                    {"Bucket": BUCKET, "Key": key, "Body": png, "ContentType": "image/png"},
                )
            with stub:
                artifacts = prepare_evidence(storage, source, result)
                stub.assert_no_pending_responses()
                self.assertTrue(body._raw_stream.closed)
            first = artifacts[0]
            png = MemoryStorage(source, raw)
            prepare_evidence(png, source, result)
            png = png.uploads[0][1]
            with Stubber(storage.internal) as stub:
                metadata = {
                    "ContentLength": len(png),
                    "ContentType": "image/png",
                    "VersionId": first.object_version,
                }
                stub.add_response(
                    "head_object", metadata, {"Bucket": BUCKET, "Key": first.object_key}
                )
                stub.add_response(
                    "get_object",
                    {**metadata, "Body": StreamingBody(io.BytesIO(png), len(png))},
                    {"Bucket": BUCKET, "Key": first.object_key, "VersionId": first.object_version},
                )
                self.assertEqual(
                    storage.put_derivative(first.object_key, png), first.object_version
                )
                stub.assert_no_pending_responses()
        finally:
            storage.close()


if __name__ == "__main__":
    unittest.main()
