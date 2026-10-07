"""Whole-batch quality gate, decode ownership, capacities and bounded joint CPU runs."""

import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.adapters.quality import QualityThresholds
from apps.ai_inference.cpu import _decode_local
from apps.ai_inference.pipeline_cpu import PipelineOptions, _compute, run_cpu_pipeline
from tests.ai.test_cpu_runner import crashed_child, delayed_child, successful_child

REAL = all(importlib.util.find_spec(name) for name in ("numpy", "PIL", "cv2", "pyclipper"))


class PipelineProcessTests(unittest.TestCase):
    def test_timeout_and_crash_reclaimed_and_next_run_succeeds(self):
        before = {child.pid for child in multiprocessing.active_children()}
        for target, code in ((delayed_child, "AI_TIMEOUT"), (crashed_child, "MODEL_ERROR")):
            with (
                patch.dict(os.environ, APP_ENV="test"),
                patch("apps.ai_inference.pipeline_cpu._child", target),
            ):
                with self.assertRaises(AdapterError) as error:
                    run_cpu_pipeline(
                        PipelineOptions(quality_only=True, timeout_seconds=1),
                        ["unused"],
                        str(uuid4()),
                    )
                self.assertEqual(error.exception.code, code)
                self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)
        with (
            patch.dict(os.environ, APP_ENV="test"),
            patch("apps.ai_inference.pipeline_cpu._child", successful_child),
        ):
            self.assertEqual(
                run_cpu_pipeline(PipelineOptions(quality_only=True), ["x"], str(uuid4())),
                {"complete": True},
            )

    def test_invalid_configuration_fails_before_spawn(self):
        for options in (
            PipelineOptions(),
            PipelineOptions(quality_only="true"),
            PipelineOptions(quality_only=True, quality=QualityThresholds(blur_min=True)),
            PipelineOptions(quality_only=True, detection_min=float("nan")),
            PipelineOptions(quality_only=True, text_min=2),
            PipelineOptions(quality_only=True, timeout_seconds=181),
        ):
            with patch.dict(os.environ, APP_ENV="test"), self.assertRaises(AdapterError):
                run_cpu_pipeline(options, ["x"], str(uuid4()))

    def test_cli_production_refusal_no_path_leak_or_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path, output = Path(directory) / "private.png", Path(directory) / "out.json"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "apps.ai_inference.pipeline",
                    "--quality-only",
                    "--input",
                    str(path),
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
            self.assertNotIn(str(path), result.stderr)
            self.assertFalse(output.exists())


@unittest.skipUnless(REAL, "Joint pixel tests require the independent real CPU environment")
class PipelinePixelTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        from PIL import Image

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.paths = [Path(self.directory.name) / f"{i}.png" for i in range(3)]
        for path in self.paths:
            pixels = np.random.default_rng(34).integers(15, 220, (64, 128, 3), np.uint8)
            with Image.fromarray(pixels) as image:
                image.save(path)
        self.options = PipelineOptions("model", "det", "rec")
        self.run_id = str(uuid4())

    def test_one_failed_image_gates_whole_batch_and_loads_no_models(self):
        from PIL import Image

        with Image.new("RGB", (64, 32)) as image:
            image.save(self.paths[1])
        with (
            patch("apps.ai_inference.pipeline_cpu.OnnxDetector.from_path") as det,
            patch("apps.ai_inference.pipeline_cpu.OnnxOCR.from_directories") as ocr,
        ):
            report = _compute(self.options, self.paths, self.run_id)
            det.assert_not_called()
            ocr.assert_not_called()
        self.assertEqual(report["outcome"], "needs_retake")
        self.assertFalse(report["models_executed"])
        self.assertTrue(report["images"][0]["quality"]["passed"])
        self.assertEqual(report["images"][1]["quality"]["reasons"], ["blur", "dark"])
        self.assertEqual(report["artifacts"], {})
        self.assertIsNone(report["runtime"]["detector"])
        for row in report["images"]:
            self.assertFalse(row["inference_executed"])
            self.assertEqual(row["lines"], [])
            self.assertEqual(row["detections"], [])

    def test_quality_only_success_is_not_business_completion(self):
        report = _compute(PipelineOptions(quality_only=True), self.paths, self.run_id)
        self.assertEqual(report["outcome"], "quality_passed")
        self.assertTrue(report["review_required"])
        self.assertFalse(report["models_executed"])
        self.assertIsNone(report["runtime"]["ocr"])
        self.assertFalse(any(report["business_capabilities"].values()))

    def _models(self, detection_count=1, line_count=1, fail_second=False):
        class Detector:
            providers = ("CPUExecutionProvider",)
            evidence = SimpleNamespace(
                model_sha256="a" * 64, config_sha256="b" * 64, preprocessor_sha256="c" * 64
            )
            inputs = []

            def detect(self, image, threshold, *, diagnostics=None):
                if diagnostics is not None:
                    self.inputs.append(image)
                    if fail_second and len(self.inputs) == 2:
                        raise AdapterError("MODEL_ERROR", "Test output failure")
                    diagnostics.update(candidates=300, kept=detection_count)
                return [
                    {"class_id": 39, "type": "bottle", "bbox": [0, 0, 1, 1], "confidence": 0.8}
                    for _ in range(detection_count)
                ]

        class OCR:
            inputs = []

            def smoke(self):
                pass

            def recognize(self, image):
                self.inputs.append(image)
                return [
                    {
                        "text": "乙醇",
                        "confidence": 0.3,
                        "detection_confidence": 0.8,
                        "quad": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "crop_evidence": {},
                    }
                    for _ in range(line_count)
                ]

        return Detector(), OCR()

    def test_sessions_loaded_once_decode_once_shared_pixels_ids_and_cleanup(self):
        det, ocr = self._models()
        with (
            patch(
                "apps.ai_inference.pipeline_cpu.OnnxDetector.from_path", return_value=det
            ) as load_det,
            patch(
                "apps.ai_inference.pipeline_cpu.OnnxOCR.from_directories", return_value=ocr
            ) as load_ocr,
            patch("apps.ai_inference.pipeline_cpu._decode_local", wraps=_decode_local) as decode,
        ):
            report = _compute(self.options, self.paths, self.run_id)
            load_det.assert_called_once()
            load_ocr.assert_called_once()
            self.assertEqual(decode.call_count, 3)
            self.assertEqual(det.inputs, ocr.inputs)
            self.assertEqual(report["outcome"], "needs_review")
            self.assertTrue(report["models_executed"])
            self.assertFalse(any(report["business_capabilities"].values()))
            for image, row in zip(det.inputs, report["images"]):
                self.assertTrue(row["inference_executed"])
                self.assertTrue(row["lines"][0]["uncertain"])
                self.assertIsNone(row["lines"][0]["parent_detection_id"])
                self.assertIsNone(row["detections"][0]["parent_detection_id"])
                with self.assertRaises(ValueError):
                    image.getpixel((0, 0))
            again = _compute(self.options, self.paths, self.run_id)
            for left, right in zip(report["images"], again["images"]):
                self.assertEqual(left["image_id"], right["image_id"])
                self.assertEqual(left["detections"], right["detections"])
                self.assertEqual(left["lines"], right["lines"])

    def test_both_batch_capacities_fail_without_truncation_and_100_is_allowed(self):
        for detections, lines, valid in ((50, 50, True), (51, 1, False), (1, 51, False)):
            det, ocr = self._models(detections, lines)
            with (
                patch("apps.ai_inference.pipeline_cpu.OnnxDetector.from_path", return_value=det),
                patch("apps.ai_inference.pipeline_cpu.OnnxOCR.from_directories", return_value=ocr),
            ):
                if valid:
                    report = _compute(self.options, self.paths[:2], self.run_id)
                    self.assertEqual(sum(len(i["detections"]) for i in report["images"]), 100)
                    self.assertEqual(sum(len(i["lines"]) for i in report["images"]), 100)
                else:
                    with self.assertRaises(AdapterError) as error:
                        _compute(self.options, self.paths[:2], self.run_id)
                    self.assertEqual(error.exception.code, "MODEL_ERROR")

    def test_later_model_failure_closes_all_inputs_and_never_returns_partial_success(self):
        det, ocr = self._models(fail_second=True)
        with (
            patch("apps.ai_inference.pipeline_cpu.OnnxDetector.from_path", return_value=det),
            patch("apps.ai_inference.pipeline_cpu.OnnxOCR.from_directories", return_value=ocr),
        ):
            with self.assertRaises(AdapterError):
                _compute(self.options, self.paths, self.run_id)
        self.assertEqual(len(ocr.inputs), 1)
        for image in det.inputs:
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))

    def test_decode_failure_reclaims_previous_images_without_loading_models(self):
        self.paths[1].write_bytes(b"not an image")
        decoded = []

        def decode(path):
            result = _decode_local(path)
            decoded.append(result[0])
            return result

        with (
            patch("apps.ai_inference.pipeline_cpu._decode_local", side_effect=decode),
            patch("apps.ai_inference.pipeline_cpu.OnnxDetector.from_path") as load,
        ):
            with self.assertRaises(AdapterError):
                _compute(self.options, self.paths, self.run_id)
            load.assert_not_called()
        for image in decoded:
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))

    def test_real_quality_cli_output_and_existing_file_preservation(self):
        output = Path(self.directory.name) / "out.json"
        command = [
            sys.executable,
            "-m",
            "apps.ai_inference.pipeline",
            "--quality-only",
            "--input",
            str(self.paths[0]),
            "--output",
            str(output),
        ]
        first = subprocess.run(
            command, env={**os.environ, "APP_ENV": "test"}, capture_output=True, timeout=15
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        raw = output.read_bytes()
        report = json.loads(raw)
        self.assertEqual(report["outcome"], "quality_passed")
        self.assertFalse(report["models_executed"])
        second = subprocess.run(
            command, env={**os.environ, "APP_ENV": "test"}, capture_output=True, timeout=15
        )
        self.assertEqual(second.returncode, 1)
        self.assertEqual(json.loads(second.stderr)["error"]["code"], "OUTPUT_UNAVAILABLE")
        self.assertEqual(output.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
