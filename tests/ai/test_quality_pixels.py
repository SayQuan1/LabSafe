"""Real pixel goldens and independent kernel checks for quality-rgb-lap1-v1."""

import importlib.util
import unittest

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.adapters.quality import QualityThresholds, evaluate_quality, resize_shape

REAL = all(importlib.util.find_spec(name) for name in ("numpy", "PIL"))


class QualityConfigTests(unittest.TestCase):
    def test_resize_integer_rounding_and_threshold_validation(self):
        self.assertEqual(resize_shape(1025, 513), (1024, 512))
        self.assertEqual(resize_shape(513, 1025), (512, 1024))
        self.assertEqual(resize_shape(7, 3), (7, 3))
        self.assertEqual(resize_shape(1, 10000), (1, 1024))
        for dimensions in ((0, 1), (True, 1), (1.0, 2)):
            with self.assertRaises(AdapterError):
                resize_shape(*dimensions)
        for limits in (
            QualityThresholds(blur_min=True),
            QualityThresholds(blur_min=-1),
            QualityThresholds(dark_min=float("nan")),
            QualityThresholds(glare_max=float("inf")),
            QualityThresholds(dark_min=1.1),
            QualityThresholds(glare_max=-0.1),
        ):
            with self.assertRaises(AdapterError):
                limits.validate()


@unittest.skipUnless(REAL, "Quality pixel tests require the independent real CPU environment")
class QualityPixelTests(unittest.TestCase):
    def test_black_white_and_rgb_gray_goldens(self):
        from PIL import Image

        for color, brightness, glare, reasons in (
            ("black", 0, 0, ["blur", "dark"]),
            ("white", 1, 1, ["blur", "glare"]),
            ((255, 0, 0), 77 / 255, 0, ["blur"]),
            ((0, 255, 0), 149 / 255, 0, ["blur"]),
            ((0, 0, 255), 29 / 255, 0, ["blur", "dark"]),
        ):
            with self.subTest(color=color), Image.new("RGB", (1, 1), color) as image:
                scores = evaluate_quality(image)
                self.assertEqual(scores["blur_score"], 0)
                self.assertEqual(scores["brightness"], brightness)
                self.assertEqual(scores["glare_ratio"], glare)
                self.assertEqual(scores["reasons"], reasons)
                self.assertFalse(scores["passed"])

    def test_single_row_and_column_reflect101_population_variance(self):
        import numpy as np
        from PIL import Image

        pixels = np.array([[[0, 0, 0], [255, 255, 255]]], np.uint8)
        for array in (pixels, pixels.transpose(1, 0, 2)):
            with Image.fromarray(array) as image:
                scores = evaluate_quality(image)
                self.assertEqual(scores["blur_score"], 260100)
                self.assertEqual(scores["brightness"], 0.5)
                self.assertEqual(scores["glare_ratio"], 0.5)
                self.assertEqual(scores["reasons"], ["glare"])

    def test_threshold_equality_and_no_score_rounding(self):
        import math

        import numpy as np
        from PIL import Image

        with Image.fromarray(np.array([[[0] * 3, [255] * 3]], np.uint8)) as image:
            self.assertTrue(evaluate_quality(image, QualityThresholds(260100, 0.5, 0.5))["passed"])
            result = evaluate_quality(
                image,
                QualityThresholds(
                    math.nextafter(260100, math.inf),
                    math.nextafter(0.5, math.inf),
                    math.nextafter(0.5, -math.inf),
                ),
            )
            self.assertEqual(result["reasons"], ["blur", "dark", "glare"])
        with Image.new("RGB", (7, 3), (255, 0, 0)) as image:
            result = evaluate_quality(image)
            self.assertEqual(result["brightness"], 77 / 255)
            self.assertNotEqual(result["brightness"], round(77 / 255, 4))

    def test_real_pillow_resize_is_bilinear_and_input_remains_open(self):
        import numpy as np
        from PIL import Image

        from tools.design.readiness_reference import quality_resized

        pixels = np.random.default_rng(34).integers(0, 256, (1, 1025, 3), np.uint8)
        with (
            Image.fromarray(pixels) as image,
            image.resize((1024, 1), Image.Resampling.BILINEAR) as expected,
        ):
            actual = evaluate_quality(image)
            reference = quality_resized(np.asarray(expected).tolist())
            self.assertEqual(actual["analysis_size"], [1024, 1])
            for key in reference:
                self.assertAlmostEqual(actual[key], reference[key], places=8)
            self.assertEqual(image.size, (1025, 1))
            self.assertEqual(image.getpixel((0, 0)), tuple(pixels[0, 0]))

    @unittest.skipUnless(importlib.util.find_spec("cv2"), "Independent OpenCV kernel comparison")
    def test_numpy_kernel_matches_actual_opencv_on_borders_and_singletons(self):
        import cv2
        import numpy as np
        from PIL import Image

        for shape in ((1, 1, 3), (1, 7, 3), (9, 1, 3), (11, 13, 3)):
            pixels = np.random.default_rng(34).integers(0, 256, shape, np.uint8)
            rgb = pixels.astype(np.uint32)
            gray = (77 * rgb[:, :, 0] + 150 * rgb[:, :, 1] + 29 * rgb[:, :, 2] + 128) // 256
            lap = cv2.Laplacian(
                gray.astype(np.float64), cv2.CV_64F, ksize=1, borderType=cv2.BORDER_REFLECT_101
            )
            with Image.fromarray(pixels) as image:
                actual = evaluate_quality(image)
                self.assertEqual(actual["blur_score"], float(lap.var(ddof=0)))

    def test_non_rgb_is_rejected_without_silent_conversion(self):
        from PIL import Image

        for mode in ("L", "RGBA"):
            with Image.new(mode, (8, 8)) as image, self.assertRaises(AdapterError):
                evaluate_quality(image)


if __name__ == "__main__":
    unittest.main()
