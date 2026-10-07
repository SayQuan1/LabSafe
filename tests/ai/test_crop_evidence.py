"""Reconstruction, independent pixel goldens and fail-closed crop evidence."""

import copy
import importlib.util
import io
import json
import subprocess
import sys
import unittest

from packages.image_evidence.perspective import CropError, CropTransform

REAL = all(importlib.util.find_spec(name) for name in ("numpy", "PIL", "cv2"))


class CropBoundaryTests(unittest.TestCase):
    def test_import_does_not_load_models_business_or_numerical_packages(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import packages.image_evidence.perspective; "
                "assert not set(sys.modules) & {'numpy','cv2','PIL','onnxruntime','boto3',"
                "'sqlalchemy','apps.ai_inference','packages.domain'}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_schema_rejects_bool_nonfinite_size_version_and_bad_points(self):
        valid = CropTransform(((0, 0), (1, 0), (1, 1), (0, 1)), 20, 10).as_dict()
        cases = []
        for key, values in (
            ("output_width", (0, 2049, True, 2.0)),
            ("output_height", (-1, 2049)),
            ("transform_version", ("perspective-v2", None)),
            ("quad", ([], [[0, 0]] * 4, [{"x": 0, "y": 0, "z": 0}] * 4)),
        ):
            cases.extend({**valid, key: value} for value in values)
        for number in (True, float("nan"), float("inf"), -0.01, 1.01, "0"):
            bad = copy.deepcopy(valid)
            bad["quad"][0]["x"] = number
            cases.append(bad)
        cases += [{**valid, "rotation": 90}, {k: v for k, v in valid.items() if k != "quad"}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(CropError) as caught:
                CropTransform.from_dict(value)
            self.assertEqual(caught.exception.code, "SCHEMA_MISMATCH")


@unittest.skipUnless(REAL, "Crop pixels require the real CPU environment")
class CropPixelTests(unittest.TestCase):
    def test_identity_rgb_png_metadata_and_byte_replay(self):
        import numpy as np
        from PIL import Image

        from packages.image_evidence.perspective import rebuild_crop

        rgb = np.random.default_rng(35).integers(0, 256, (19, 37, 3), np.uint8)
        recipe = CropTransform(((0, 0), (1, 0), (1, 1), (0, 1)), 37, 19)
        first = rebuild_crop(rgb, recipe)
        restored = CropTransform.from_dict(json.loads(json.dumps(recipe.as_dict())))
        second = rebuild_crop(rgb, restored, expected_sha256=first.sha256)
        np.testing.assert_array_equal(first.pixels, rgb)
        self.assertEqual(first.png, second.png)
        with Image.open(io.BytesIO(first.png)) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.info, {})
            np.testing.assert_array_equal(np.asarray(image), rgb)

    def test_subrectangle_pixel_golden_uses_width_minus_one_on_non_square_source(self):
        import numpy as np

        from packages.image_evidence.perspective import perspective_rgb

        y, x = np.indices((9, 13))
        rgb = np.stack((x * 10, y * 20, x + y), axis=-1).astype(np.uint8)
        recipe = CropTransform(
            ((2 / 12, 1 / 8), (8 / 12, 1 / 8), (8 / 12, 5 / 8), (2 / 12, 5 / 8)), 7, 5
        )
        np.testing.assert_array_equal(perspective_rgb(rgb, recipe), rgb[1:6, 2:9])

    def test_skew_crop_matches_independent_opencv_geometry(self):
        import cv2
        import numpy as np

        from packages.image_evidence.perspective import perspective_rgb

        rgb = np.random.default_rng(36).integers(0, 256, (70, 130, 3), np.uint8)
        source = np.array([[5, 2], [118, 14], [99, 60], [14, 49]], np.float32)
        target = np.array([[0, 0], [99, 0], [99, 39], [0, 39]], np.float32)
        expected = cv2.warpPerspective(
            rgb,
            cv2.getPerspectiveTransform(source, target),
            (100, 40),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0),
        )
        recipe = CropTransform(
            tuple(map(tuple, (source.astype(np.float64) / [129, 69]).tolist())), 100, 40
        )
        np.testing.assert_array_equal(perspective_rgb(rgb, recipe), expected)

    def test_invalid_geometry_singular_one_pixel_and_rgb_rejected(self):
        import numpy as np

        from packages.image_evidence.perspective import perspective_rgb

        rgb = np.zeros((40, 60, 3), np.uint8)
        for quad in (
            ((0, 0), (1, 1), (1, 0), (0, 1)),
            ((0, 0), (0, 1), (1, 1), (1, 0)),
            ((0, 0), (1, 0), (1, 0), (0, 1)),
            ((0, 0), (0.001, 0), (0.001, 0.001), (0, 0.001)),
        ):
            with self.subTest(quad=quad), self.assertRaises(CropError) as caught:
                perspective_rgb(rgb, CropTransform(quad, 10, 10))
            self.assertEqual(caught.exception.code, "MODEL_ERROR")
        for size in ((1, 10), (10, 1)):
            with self.assertRaises(CropError):
                perspective_rgb(rgb, CropTransform(((0, 0), (1, 0), (1, 1), (0, 1)), *size))
        for bad in (rgb.astype(np.float32), rgb[:, :, 0], np.zeros((1, 10, 3), np.uint8)):
            with self.assertRaises(CropError):
                perspective_rgb(bad, CropTransform(((0, 0), (1, 0), (1, 1), (0, 1)), 10, 10))

    def _ocr_crop(self, tall=False):
        import numpy as np

        from apps.ai_inference.adapters.ocrv6 import crop_with_evidence
        from packages.image_evidence.perspective import rgb_digest

        rgb = np.random.default_rng(35).integers(0, 256, (90, 140, 3), np.uint8)
        points = np.array(
            [[10, 5], [30, 5], [30, 75], [10, 75]]
            if tall
            else [[10, 5], [125, 5], [125, 40], [10, 40]],
            np.float32,
        )
        recognized, metadata = crop_with_evidence(rgb, points)
        metadata["source_rgb_sha256"] = rgb_digest(rgb)
        return rgb, recognized, metadata

    def test_serialized_tall_evidence_rebuild_preserves_recognition_pixels(self):
        import numpy as np

        from packages.image_evidence.perspective import rebuild_ocr_evidence

        rgb, recognized, metadata = self._ocr_crop(tall=True)
        restored = json.loads(json.dumps(metadata))
        evidence = rebuild_ocr_evidence(rgb, restored)
        self.assertEqual(metadata["recognition_rotation_ccw"], 90)
        self.assertEqual(evidence.pixels.shape, (70, 20, 3))
        np.testing.assert_array_equal(recognized, np.rot90(evidence.pixels))
        self.assertTrue(recognized.flags.c_contiguous)

    def test_ocr_recognition_tensor_is_derived_from_the_reported_evidence(self):
        from unittest.mock import patch

        import numpy as np
        from PIL import Image

        from apps.ai_inference.adapters.ocrv6 import OnnxOCR, rec_tensor
        from packages.image_evidence.perspective import rebuild_ocr_evidence, recognition_rgb

        rgb = np.random.default_rng(37).integers(0, 256, (90, 140, 3), np.uint8)
        points = np.array([[10, 5], [30, 5], [30, 75], [10, 75]], np.float32)
        ocr = OnnxOCR.__new__(OnnxOCR)
        ocr.det, ocr.rec = object(), object()
        ocr.characters = ("", "a", *("" for _ in range(18708)))
        fed = []

        def run(session, tensor):
            if session is ocr.det:
                return np.zeros((1, 1, *tensor.shape[2:]), np.float32)
            fed.append(tensor.copy())
            output = np.zeros((1, tensor.shape[3] // 8, 18710), np.float32)
            output[:, :, 0] = 1
            output[0, 1, 0], output[0, 1, 1] = 0, 1
            return output

        with (
            Image.fromarray(rgb) as image,
            patch.object(ocr, "_run", side_effect=run),
            patch(
                "apps.ai_inference.adapters.ocrv6.db_regions",
                return_value=[{"quad_pixels": points, "detection_confidence": 0.8}],
            ),
        ):
            rows = ocr.recognize(image)
        self.assertEqual(rows[0]["text"], "a")
        metadata = json.loads(json.dumps(rows[0]["crop_evidence"]))
        rebuilt = rebuild_ocr_evidence(rgb, metadata)
        oriented = recognition_rgb(rebuilt.pixels, metadata["recognition_rotation_ccw"])
        np.testing.assert_array_equal(fed[0], rec_tensor(oriented))

    def test_tampered_source_recipe_png_rotation_and_recognition_digest_fail_closed(self):
        from packages.image_evidence.perspective import rebuild_ocr_evidence

        rgb, _, metadata = self._ocr_crop()
        for key, value in (
            ("source_rgb_sha256", "0" * 64),
            ("png_sha256", "0" * 64),
            ("png_size_bytes", 1),
            ("recognition_rgb_sha256", "0" * 64),
            ("recognition_width", 1),
            ("recognition_rotation_ccw", 180),
            ("recognition_rotation_ccw", 90),
            ("recognition_rotation_ccw", False),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(CropError):
                rebuild_ocr_evidence(rgb, {**metadata, key: value})
        bad = copy.deepcopy(metadata)
        bad["recipe"]["quad"][0]["x"] += 0.01
        with self.assertRaises(CropError) as caught:
            rebuild_ocr_evidence(rgb, bad)
        self.assertEqual(caught.exception.code, "HASH_MISMATCH")
        with self.assertRaises(CropError):
            rebuild_ocr_evidence(rgb, {})
        modified = rgb.copy()
        modified[0, 0, 0] ^= 255
        with self.assertRaises(CropError) as caught:
            rebuild_ocr_evidence(modified, metadata)
        self.assertEqual(caught.exception.code, "HASH_MISMATCH")


if __name__ == "__main__":
    unittest.main()
