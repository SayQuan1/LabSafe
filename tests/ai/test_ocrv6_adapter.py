"""CPU OCR numerical, geometry, vocabulary and bounded execution checks."""

import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.adapters.ocrv6 import (
    crop_region,
    db_regions,
    decode_ctc,
    det_tensor,
    load_artifacts,
    rec_tensor,
    validate_session,
)
from apps.ai_inference.ocr_cpu import OcrOptions, _compute, run_cpu_ocr
from tests.ai.test_cpu_runner import crashed_child, delayed_child, successful_child

REAL = all(importlib.util.find_spec(name) for name in ("numpy", "PIL", "cv2", "pyclipper"))


class OcrProcessTests(unittest.TestCase):
    def test_ocr_timeout_and_crash_reclaimed_and_next_run_succeeds(self):
        before = {child.pid for child in multiprocessing.active_children()}
        for target, code in ((delayed_child, "AI_TIMEOUT"), (crashed_child, "MODEL_ERROR")):
            with (
                patch.dict(os.environ, APP_ENV="test"),
                patch("apps.ai_inference.ocr_cpu._child", target),
            ):
                with self.assertRaises(AdapterError) as error:
                    run_cpu_ocr(OcrOptions("det", "rec", timeout_seconds=1), ["x"], str(uuid4()))
                self.assertEqual(error.exception.code, code)
                self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)
        with (
            patch.dict(os.environ, APP_ENV="test"),
            patch("apps.ai_inference.ocr_cpu._child", successful_child),
        ):
            self.assertEqual(
                run_cpu_ocr(OcrOptions("d", "r"), ["x"], str(uuid4())), {"complete": True}
            )

    def test_production_refused_without_output_or_path_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            private = Path(directory) / "private.png"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "apps.ai_inference.ocr",
                    "--det-dir",
                    "det",
                    "--rec-dir",
                    "rec",
                    "--input",
                    str(private),
                    "--output",
                    str(output),
                ],
                env={**os.environ, "APP_ENV": "production"},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stderr)["error"]["code"], "VALIDATION_ERROR")
            self.assertNotIn(str(private), result.stderr)
            self.assertFalse(output.exists())

    def test_invalid_threshold_threads_deadline_and_image_count(self):
        with patch.dict(os.environ, APP_ENV="test"):
            for options in (
                OcrOptions("d", "r", text_min=float("nan")),
                OcrOptions("d", "r", text_min=True),
                OcrOptions("d", "r", threads=0),
                OcrOptions("d", "r", timeout_seconds=181),
            ):
                with self.assertRaises(AdapterError):
                    run_cpu_ocr(options, ["x"], str(uuid4()))
            with self.assertRaises(AdapterError):
                run_cpu_ocr(OcrOptions("d", "r"), ["x"] * 4, str(uuid4()))


@unittest.skipUnless(REAL, "OCR numerical tests require the independent real CPU environment")
class OcrNumericalTests(unittest.TestCase):
    def test_ctc_blank_duplicates_space_and_unicode(self):
        import numpy as np

        characters = ("", "乙", "醇", " ")
        indices = [0, 1, 1, 0, 1, 2, 2, 3, 3, 0]
        probs = np.zeros((1, len(indices), len(characters)), np.float32)
        for step, index in enumerate(indices):
            probs[0, step, index] = 0.9
            probs[0, step, (index + 1) % 4] = 0.1
        text, confidence = decode_ctc(probs, characters)
        self.assertEqual(text, "乙乙醇 ")
        self.assertAlmostEqual(confidence, 0.9, places=6)
        blank = np.zeros((1, 4, 4), np.float32)
        blank[:, :, 0] = 1
        self.assertEqual(decode_ctc(blank, characters), ("", 0))

    def test_ctc_nonfinite_bad_dtype_shape_and_probability_rejected(self):
        import numpy as np

        valid = np.zeros((1, 4, 3), np.float32)
        valid[:, :, 0] = 1
        cases = [
            valid.astype(np.float64),
            valid[:, :, :2],
            valid * 0.5,
            np.full_like(valid, np.nan),
            np.full_like(valid, np.inf),
            valid * 2,
        ]
        for probs in cases:
            with self.subTest(shape=probs.shape), self.assertRaises(AdapterError):
                decode_ctc(probs, ("", "a", " "))

    def test_bgr_normalization_padding_and_rec_width_limit(self):
        import numpy as np

        rgb = np.full((32, 64, 3), [255, 0, 0], np.uint8)
        det = det_tensor(rgb)
        self.assertEqual(det.shape, (1, 3, 32, 64))
        self.assertEqual(det.dtype, np.float32)
        self.assertTrue(det.flags.c_contiguous)
        np.testing.assert_allclose(
            det[0, :, 0, 0], [-0.485 / 0.229, -0.456 / 0.224, (1 - 0.406) / 0.225], atol=1e-6
        )
        rec = rec_tensor(rgb)
        self.assertEqual(rec.shape, (1, 3, 48, 320))
        np.testing.assert_array_equal(rec[0, :, 0, 0], [-1, -1, 1])
        self.assertTrue((rec[0, :, :, 96:] == 0).all())
        wide = rec_tensor(np.zeros((48, 333, 3), np.uint8))
        self.assertEqual(wide.shape[-1], 336)
        self.assertTrue((wide[0, :, :, 333:] == 0).all())
        with self.assertRaises(AdapterError):
            rec_tensor(np.zeros((2, 200, 3), np.uint8))

    def test_db_non_square_quad_bounds_sort_and_blank(self):
        import numpy as np

        probs = np.zeros((1, 1, 96, 192), np.float32)
        self.assertEqual(db_regions(probs, (384, 192)), [])
        probs[0, 0, 10:20, 15:70] = 0.9
        probs[0, 0, 60:70, 95:170] = 0.8
        rows = db_regions(probs, (384, 192))
        self.assertEqual(len(rows), 2)
        self.assertLess(rows[0]["quad_pixels"][0, 1], rows[1]["quad_pixels"][0, 1])
        for row in rows:
            quad = row["quad_pixels"]
            self.assertTrue((quad >= 0).all())
            self.assertTrue((quad[:, 0] <= 383).all())
            self.assertTrue((quad[:, 1] <= 191).all())
            self.assertLess(quad[0, 0], quad[1, 0])
            self.assertLess(quad[0, 1], quad[3, 1])
        with self.assertRaises(AdapterError):
            db_regions(np.full_like(probs, np.nan), (384, 192))

    def test_db_capacity_is_error_not_truncation(self):
        import numpy as np

        probs = np.zeros((1, 1, 240, 400), np.float32)
        for index in range(101):
            y, x = 5 + index // 20 * 30, 5 + index % 20 * 20
            probs[0, 0, y : y + 6, x : x + 6] = 1
        with self.assertRaises(AdapterError) as error:
            db_regions(probs, (400, 240))
        self.assertEqual(error.exception.code, "MODEL_ERROR")
        probs[0, 0, y : y + 6, x : x + 6] = 0
        self.assertEqual(len(db_regions(probs, (400, 240))), 100)

    def test_perspective_crop_and_vertical_rotation_preserve_pixels(self):
        import numpy as np

        rgb = np.full((80, 160, 3), [250, 10, 3], np.uint8)
        quad = np.array([[10, 10], [110, 10], [110, 40], [10, 40]], np.float32)
        crop = crop_region(rgb, quad)
        self.assertEqual(crop.shape, (30, 100, 3))
        np.testing.assert_array_equal(crop[15, 50], [250, 10, 3])
        tall = np.array([[10, 10], [20, 10], [20, 60], [10, 60]], np.float32)
        self.assertEqual(crop_region(rgb, tall).shape, (10, 50, 3))
        with self.assertRaises(AdapterError):
            crop_region(rgb, np.zeros((4, 2), np.float32))
        for invalid in (
            quad[[0, 2, 1, 3]],
            np.array([[10, 10], [10, 10], [110, 40], [10, 40]], np.float32),
            np.full((4, 2), np.nan, np.float32),
        ):
            with self.assertRaises(AdapterError):
                crop_region(rgb, invalid)

    def test_artifact_hash_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "inference.onnx").write_bytes(b"untrusted")
            with self.assertRaises(AdapterError) as error:
                load_artifacts(directory, "det")
            self.assertEqual(error.exception.code, "HASH_MISMATCH")

    def test_wrong_signature_or_provider_rejected(self):
        from types import SimpleNamespace

        class Session:
            output = SimpleNamespace(
                name="fetch_name_0", type="tensor(float)", shape=["b", "t", 18710]
            )
            providers = ["CPUExecutionProvider"]

            def get_inputs(self):
                return [SimpleNamespace(name="x", type="tensor(float)", shape=["b", 3, 48, "w"])]

            def get_outputs(self):
                return [self.output]

            def get_providers(self):
                return self.providers

        session = Session()
        validate_session(session, "rec")
        session.providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        with self.assertRaises(AdapterError):
            validate_session(session, "rec")
        session.providers = ["CPUExecutionProvider"]
        session.output.shape = ["b", "t", 18709]
        with self.assertRaises(AdapterError):
            validate_session(session, "rec")

    def test_report_ids_repeat_low_confidence_kept_no_chemical_facts_and_batch_capacity(self):
        from PIL import Image

        class OCR:
            count = 1

            def smoke(self):
                pass

            def recognize(self, image):
                return [
                    {
                        "text": "乙醇",
                        "confidence": 0.3,
                        "detection_confidence": 0.8,
                        "quad": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "crop_evidence": {},
                    }
                    for _ in range(self.count)
                ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            with Image.new("RGB", (200, 100)) as image:
                image.save(path)
            ocr, run_id = OCR(), str(uuid4())
            with patch(
                "apps.ai_inference.ocr_cpu.OnnxOCR.from_directories", return_value=ocr
            ) as load:
                first = _compute(OcrOptions("d", "r"), [path], run_id)
                second = _compute(OcrOptions("d", "r"), [path], run_id)
                self.assertEqual(first["images"][0]["lines"], second["images"][0]["lines"])
                row = first["images"][0]["lines"][0]
                self.assertTrue(row["uncertain"])
                self.assertIsNone(row["parent_detection_id"])
                self.assertFalse(first["business_capabilities"]["chemical_entities"])
                ocr.count = 50
                report = _compute(OcrOptions("d", "r"), [path, path], run_id)
                self.assertEqual(sum(len(i["lines"]) for i in report["images"]), 100)
                ocr.count = 51
                with self.assertRaises(AdapterError):
                    _compute(OcrOptions("d", "r"), [path, path], run_id)
                self.assertEqual(load.call_count, 4)


if __name__ == "__main__":
    unittest.main()
