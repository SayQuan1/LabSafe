"""Golden geometry for text-bottle-quad80-v1; no model or ORM dependencies."""

import copy
import math
from uuid import uuid4

import pytest

from packages.inference_protocol.association import associate_rows, select_bottle


def point_quad(x1=0.2, y1=0.2, x2=0.8, y2=0.4):
    return [
        {"x": x1, "y": y1},
        {"x": x2, "y": y1},
        {"x": x2, "y": y2},
        {"x": x1, "y": y2},
    ]


def detection(image, key, bbox, *, class_id=39, kind="bottle"):
    return {
        "image_id": image,
        "detection_id": key,
        "class_id": class_id,
        "type": kind,
        "bbox": bbox,
        "confidence": 0.9,
        "parent_detection_id": None,
    }


def test_unique_smallest_same_image_bottle_is_selected():
    image, first, second = str(uuid4()), str(uuid4()), str(uuid4())
    detections = [
        detection(image, first, [0, 0, 1, 1]),
        detection(image, second, [0.1, 0.1, 0.9, 0.9]),
    ]
    assert select_bottle(point_quad(), image, detections) == second


def test_no_bottle_cross_image_wrong_class_or_insufficient_overlap_is_unknown():
    image, other = str(uuid4()), str(uuid4())
    quad = point_quad()
    assert select_bottle(quad, image, [detection(other, str(uuid4()), [0, 0, 1, 1])]) is None
    assert (
        select_bottle(
            quad, image, [detection(image, str(uuid4()), [0, 0, 1, 1], class_id=40, kind="chair")]
        )
        is None
    )
    assert (
        select_bottle(
            point_quad(0.2, 0.2, 0.8, 0.4),
            image,
            [detection(image, str(uuid4()), [0, 0.2, 0.5, 0.4])],
        )
        is None
    )


def test_equal_smallest_bottles_do_not_use_id_tiebreak():
    image = str(uuid4())
    rows = [detection(image, key, [0, 0, 1, 1]) for key in (str(uuid4()), str(uuid4()))]
    assert select_bottle(point_quad(), image, rows) is None


def test_text_vertices_on_box_boundary_and_exact_eighty_percent_are_accepted():
    image, key = str(uuid4()), str(uuid4())
    assert select_bottle(point_quad(), image, [detection(image, key, [0.2, 0.2, 0.8, 0.4])]) == key
    quad = point_quad(0, 0, 1, 1)
    assert select_bottle(quad, image, [detection(image, key, [0, 0, 0.8, 1])]) == key
    assert (
        select_bottle(quad, image, [detection(image, key, [0, 0, math.nextafter(0.8, 0), 1])])
        is None
    )


def test_tilted_quad_uses_polygon_area_instead_of_enclosing_rectangle():
    image, key = str(uuid4()), str(uuid4())
    diamond = [{"x": x, "y": y} for x, y in ((0.5, 0), (1, 0.5), (0.5, 1), (0, 0.5))]
    # Actual diamond intersection is 7/8; bbox approximation would be 3/4.
    assert select_bottle(diamond, image, [detection(image, key, [0, 0, 0.75, 1])]) == key


def test_skew_quad_uses_actual_area_and_vertex_center():
    image, key = str(uuid4()), str(uuid4())
    trapezoid = [{"x": x, "y": y} for x, y in ((0, 0), (1, 0), (0.2, 1), (0, 1))]
    # Clip off the top tenth: area ratio > 80%, vertex mean y=0.5 is inside.
    assert select_bottle(trapezoid, image, [detection(image, key, [0, 0.1, 1, 1])]) == key


def test_input_order_and_confidence_do_not_break_equal_area_ties():
    image = str(uuid4())
    rows = [
        detection(image, str(uuid4()), [0, 0, 1, 1]),
        detection(image, str(uuid4()), [0, 0, 1, 1]),
    ]
    rows[0]["confidence"] = 0.99
    rows[1]["confidence"] = 0.01
    for order in (rows, list(reversed(rows))):
        assert select_bottle(point_quad(), image, order) is None


@pytest.mark.parametrize(
    "bounds", [[0, 0, 0, 1], [1, 0, 0, 1], [0, 0, float("nan"), 1], [0, 0, True, 1]]
)
def test_invalid_detection_geometry_rejected(bounds):
    with pytest.raises(ValueError):
        select_bottle(point_quad(), "image", [detection("image", "key", bounds)])


@pytest.mark.parametrize("case", ["concave", "crossed", "counterclockwise", "nonfinite", "outside"])
def test_invalid_quad_geometry_rejected(case):
    quad = point_quad()
    if case == "concave":
        quad[2] = {"x": 0.3, "y": 0.25}
    elif case == "crossed":
        quad[1], quad[2] = quad[2], quad[1]
    elif case == "counterclockwise":
        quad.reverse()
    elif case == "nonfinite":
        quad[0]["x"] = float("inf")
    else:
        quad[0]["x"] = -0.1
    with pytest.raises(ValueError):
        select_bottle(quad, "image", [])


@pytest.mark.parametrize("quad", [[], point_quad(0.8, 0.2, 0.2, 0.4), [{"x": 0, "y": 0}] * 4])
def test_bad_quad_rejected_without_partial_mutation(quad):
    image, key = str(uuid4()), str(uuid4())
    rows = [
        {
            "image_id": image,
            "crop_evidence": {"recipe": {"quad": quad}},
            "parent_detection_id": None,
        }
    ]
    before = copy.deepcopy(rows)
    with pytest.raises(ValueError):
        associate_rows(rows, [detection(image, key, [0, 0, 1, 1])])
    assert rows == before


def test_associate_rows_all_or_nothing_and_stable():
    image, first, second = str(uuid4()), str(uuid4()), str(uuid4())
    rows = [
        {
            "image_id": image,
            "crop_evidence": {"recipe": {"quad": point_quad()}},
            "parent_detection_id": "stale",
        },
        {
            "image_id": image,
            "crop_evidence": {"recipe": {"quad": point_quad(0.1, 0.5, 0.9, 0.7)}},
            "parent_detection_id": "stale",
        },
    ]
    detections = [detection(image, first, [0, 0, 1, 1]), detection(image, second, [0, 0, 1, 1])]
    associate_rows(rows, detections)
    assert rows[0]["parent_detection_id"] is None and rows[1]["parent_detection_id"] is None
    detections.pop()
    associate_rows(rows, detections)
    assert rows[0]["parent_detection_id"] == first and rows[1]["parent_detection_id"] == first
    before = copy.deepcopy(rows)
    rows[1]["crop_evidence"]["recipe"]["quad"] = []
    with pytest.raises(ValueError):
        associate_rows(rows, [])
    assert rows[0] == before[0]
    assert rows[1]["parent_detection_id"] == first


def test_limits_and_duplicate_detection_rejected():
    image = str(uuid4())
    with pytest.raises(ValueError):
        select_bottle(point_quad(), image, [detection(image, "same", [0, 0, 1, 1])] * 2)
    rows = [
        {
            "image_id": image,
            "crop_evidence": {"recipe": {"quad": point_quad()}},
            "parent_detection_id": None,
        }
    ] * 101
    with pytest.raises(ValueError):
        associate_rows(rows, [])
