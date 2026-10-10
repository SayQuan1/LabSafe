"""Independent OpenCV polygon checks and real crop bytes; no model accuracy claim."""

import importlib.util
import math
import unittest

from packages.inference_protocol.association import area, intersection_area, polygon, select_bottle

HAS_PIXELS = all(importlib.util.find_spec(name) for name in ("numpy", "cv2", "PIL"))


@unittest.skipUnless(HAS_PIXELS, "Requires isolated real pixel runtime")
class AssociationPixelsTests(unittest.TestCase):
    def test_actual_quad_intersections_match_independent_opencv(self):
        import cv2
        import numpy as np

        for angle in (0, 13, 30, 45, 60, 87):
            radians = math.radians(angle)
            corners = []
            for x, y in ((-0.3, -0.1), (0.3, -0.1), (0.3, 0.1), (-0.3, 0.1)):
                corners.append(
                    (
                        0.5 + x * math.cos(radians) - y * math.sin(radians),
                        0.5 + x * math.sin(radians) + y * math.cos(radians),
                    )
                )
            points = polygon([{"x": x, "y": y} for x, y in corners])
            quad = np.asarray(points, np.float32)
            self.assertAlmostEqual(area(points), cv2.contourArea(quad), places=7)
            for bounds in (
                (0, 0, 1, 1),
                (0, 0, 0.5, 1),
                (0.35, 0.3, 0.7, 0.7),
                (0.8, 0.8, 1, 1),
                (0, 0, 0.75, 1),
            ):
                x1, y1, x2, y2 = bounds
                box = np.array(((x1, y1), (x2, y1), (x2, y2), (x1, y2)), np.float32)
                expected, _ = cv2.intersectConvexConvex(quad, box)
                self.assertAlmostEqual(intersection_area(points, bounds), expected, places=7)

    def test_tilted_crop_digests_and_ids_unaffected_by_association(self):
        import numpy as np

        from packages.image_evidence.perspective import CropTransform, rebuild_crop

        y, x = np.indices((80, 120))
        rgb = np.stack((x * 2, y * 3, x + y), axis=-1).astype(np.uint8)
        recipe = CropTransform(((0.2, 0.3), (0.8, 0.2), (0.85, 0.6), (0.25, 0.7)), 80, 35)
        before = rebuild_crop(rgb, recipe)
        detection = {
            "image_id": "same",
            "detection_id": "bottle",
            "class_id": 39,
            "type": "bottle",
            "bbox": [0.1, 0.1, 0.9, 0.8],
        }
        self.assertEqual(select_bottle(recipe.as_dict()["quad"], "same", [detection]), "bottle")
        after = rebuild_crop(rgb, recipe)
        self.assertEqual(before.sha256, after.sha256)
        self.assertEqual(before.png, after.png)
        self.assertTrue(np.array_equal(before.pixels, after.pixels))


if __name__ == "__main__":
    unittest.main()
