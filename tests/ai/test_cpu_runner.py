import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.cpu import CpuOptions, _compute, _decode_local, run_cpu_detection

try:
    from PIL import Image
except ImportError:
    Image = None


def delayed_child(channel, *args):
    time.sleep(10)
    channel.send(("result", {"late": True}))
    channel.close()


def successful_child(channel, *args):
    channel.send(("result", {"complete": True}))
    channel.close()


def crashed_child(channel, *args):
    channel.close()


class CpuProcessTests(unittest.TestCase):
    def test_timeout_kills_child_and_next_run_can_succeed(self):
        before = {child.pid for child in multiprocessing.active_children()}
        options = CpuOptions("model", "config", "preprocessor", timeout_seconds=1)
        started = time.monotonic()
        with (
            patch.dict(os.environ, APP_ENV="test"),
            patch("apps.ai_inference.cpu._child", delayed_child),
        ):
            with self.assertRaises(AdapterError) as error:
                run_cpu_detection(options, ["unused.png"], str(uuid4()))
        self.assertEqual(error.exception.code, "AI_TIMEOUT")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)
        with (
            patch.dict(os.environ, APP_ENV="test"),
            patch("apps.ai_inference.cpu._child", successful_child),
        ):
            self.assertEqual(
                run_cpu_detection(CpuOptions("m", "c", "p"), ["unused.png"], str(uuid4())),
                {"complete": True},
            )

    def test_child_crash_has_no_success_or_orphan(self):
        before = {child.pid for child in multiprocessing.active_children()}
        with (
            patch.dict(os.environ, APP_ENV="test"),
            patch("apps.ai_inference.cpu._child", crashed_child),
        ):
            with self.assertRaises(AdapterError) as error:
                run_cpu_detection(CpuOptions("m", "c", "p"), ["unused.png"], str(uuid4()))
        self.assertEqual(error.exception.code, "MODEL_ERROR")
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, before)

    def test_config_and_environment_fail_before_spawn(self):
        for options in (
            CpuOptions("m", "c", "p", detection_min=float("nan")),
            CpuOptions("m", "c", "p", detection_min=True),
            CpuOptions("m", "c", "p", threads=0),
            CpuOptions("m", "c", "p", timeout_seconds=float("inf")),
            CpuOptions("m", "c", "p", timeout_seconds=181),
        ):
            with self.subTest(options=options), patch.dict(os.environ, APP_ENV="test"):
                with self.assertRaises(AdapterError):
                    run_cpu_detection(options, ["unused.png"], str(uuid4()))
        with patch.dict(os.environ, APP_ENV="production"):
            with self.assertRaises(AdapterError):
                run_cpu_detection(CpuOptions("m", "c", "p"), ["unused.png"], str(uuid4()))
        with patch.dict(os.environ, APP_ENV="test"):
            for paths, run_id in (([], str(uuid4())), (["x"] * 4, str(uuid4())), (["x"], "bad")):
                with self.assertRaises(AdapterError):
                    run_cpu_detection(CpuOptions("m", "c", "p"), paths, run_id)

    def test_cli_failure_does_not_write_report_or_expose_input_path(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            secret_path = Path(directory) / "private-image.png"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "apps.ai_inference.detect",
                    "--model",
                    "missing.onnx",
                    "--input",
                    str(secret_path),
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
            self.assertNotIn(str(secret_path), result.stderr)
            self.assertFalse(output.exists())


@unittest.skipIf(Image is None, "Real CPU image tests require requirements/py311-ai-real.txt")
class CpuImageTests(unittest.TestCase):
    def test_decode_normalizes_exif_once_and_rejects_animation_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "original.jpg"
            exif = Image.Exif()
            exif[274] = 6
            with Image.new("RGB", (80, 40)) as original:
                original.save(path, exif=exif)
            image, digest = _decode_local(path)
            self.assertEqual(image.size, (40, 80))
            self.assertEqual(len(digest), 64)
            self.assertEqual(image.mode, "RGB")
            image.close()
            path.write_bytes(b"not-an-image")
            with self.assertRaises(AdapterError):
                _decode_local(path)
            with Image.new("RGB", (8, 8)) as first, Image.new("RGB", (8, 8), "red") as second:
                first.save(root / "animation.png", save_all=True, append_images=[second])
            with self.assertRaises(AdapterError):
                _decode_local(root / "animation.png")

    def test_model_loaded_once_ids_stable_unknown_business_and_batch_capacity(self):
        class Detector:
            providers = ("CPUExecutionProvider",)
            evidence = SimpleNamespace(
                model_sha256="a" * 64, config_sha256="b" * 64, preprocessor_sha256="c" * 64
            )
            count = 1

            def detect(self, image, threshold, *, diagnostics=None):
                if diagnostics is not None:
                    diagnostics.update(candidates=300, kept=self.count)
                return [
                    {"class_id": 0, "type": "person", "bbox": [0, 0, 1, 1], "confidence": 0.8}
                    for _ in range(self.count)
                ]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            with Image.new("RGB", (80, 40)) as image:
                image.save(path)
            options = CpuOptions("m", "c", "p")
            run_id = str(uuid4())
            detector = Detector()
            with (
                patch(
                    "apps.ai_inference.cpu.OnnxDetector.from_path", return_value=detector
                ) as load,
                patch("apps.ai_inference.cpu.version", return_value="test"),
            ):
                result = _compute(options, [path, path], run_id)
                load.assert_called_once()
                again = _compute(options, [path, path], run_id)
                self.assertEqual(
                    result["images"][0]["detections"], again["images"][0]["detections"]
                )
                self.assertNotEqual(
                    result["images"][0]["image_id"], result["images"][1]["image_id"]
                )
                self.assertTrue(result["review_required"])
                self.assertFalse(result["is_simulated"])
                self.assertFalse(result["business_capabilities"]["chemical_entities"])
                self.assertIsNone(result["images"][0]["detections"][0]["parent_detection_id"])
                detector.count = 60
                with self.assertRaises(AdapterError) as error:
                    _compute(options, [path, path], run_id)
                self.assertEqual(error.exception.code, "MODEL_ERROR")


if __name__ == "__main__":
    unittest.main()
