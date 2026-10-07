import unittest

from apps.ai_inference.adapters.dfine import AdapterError, postprocess_project, preprocess_rgb

try:
    import numpy as np
    from PIL import Image
except ImportError:
    np = Image = None


@unittest.skipIf(np is None, "Pixel tests require the independent real CPU runtime")
class CpuPixelTests(unittest.TestCase):
    def test_non_square_rgb_chw_float32_and_height_width_target(self):
        source = np.zeros((400, 800, 3), dtype=np.uint8)
        source[:, :400, 0] = 255
        source[:, 400:, 2] = 255
        with Image.fromarray(source) as image:
            tensor, sizes = preprocess_rgb(image)
        self.assertEqual(tensor.shape, (1, 3, 640, 640))
        self.assertEqual(str(tensor.dtype), "float32")
        self.assertTrue(tensor.flags.c_contiguous)
        self.assertEqual(sizes.tolist(), [[400, 800]])
        self.assertEqual(str(sizes.dtype), "int64")
        self.assertEqual(tensor[0, :, 0, 0].tolist(), [1.0, 0.0, 0.0])
        self.assertEqual(tensor[0, :, 0, -1].tolist(), [0.0, 0.0, 1.0])

    def test_grayscale_is_not_silently_accepted(self):
        with Image.new("L", (80, 40)) as image:
            with self.assertRaises(AdapterError):
                preprocess_rgb(image)

    def test_raw_tensor_nonfinite_and_no_silent_truncation(self):
        logits = np.full((1, 300, 80), -20, dtype=np.float32)
        boxes = np.full((1, 300, 4), 0.5, dtype=np.float32)
        logits[0, :100, 39] = 8
        self.assertEqual(len(postprocess_project(logits, boxes, [800, 400], 0.4)), 100)
        logits[0, 100, 39] = 8
        with self.assertRaises(AdapterError):
            postprocess_project(logits, boxes, [800, 400], 0.4)
        logits[0, 100, 39] = -20
        boxes[0, 299, 0] = np.nan
        with self.assertRaises(AdapterError):
            postprocess_project(logits, boxes, [800, 400], 0.4)


if __name__ == "__main__":
    unittest.main()
