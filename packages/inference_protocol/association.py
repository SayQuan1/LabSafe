"""Deterministic text-quad to COCO bottle geometry, without numerical dependencies."""

import math

ALGORITHM_ID = "text-bottle-quad80-v1"
BOTTLE_CLASS_ID = 39
MAX_REGIONS = 100
MAX_DETECTIONS = 100


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def box(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or not all(_number(v) for v in value)
    ):
        raise ValueError("Invalid normalized detection box")
    x1, y1, x2, y2 = value
    if x1 >= x2 or y1 >= y2:
        raise ValueError("Detection box must have positive area")
    return x1, y1, x2, y2


def polygon(quad):
    if not isinstance(quad, (list, tuple)) or len(quad) != 4:
        raise ValueError("Text needs four normalized points")
    points = []
    for point in quad:
        if (
            not isinstance(point, dict)
            or set(point) != {"x", "y"}
            or not all(_number(point[k]) for k in ("x", "y"))
        ):
            raise ValueError("Invalid normalized text point")
        points.append((point["x"], point["y"]))
    crosses = []
    for i in range(4):
        a, b, c = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        crosses.append((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    if not all(v > 0 for v in crosses):
        raise ValueError("Text quad must be convex and clockwise in image coordinates")
    return points


def area(points):
    # Fan triangles avoid cancellation from large absolute image coordinates.
    if len(points) < 3:
        return 0.0
    origin = points[0]
    return (
        abs(
            math.fsum(
                (points[i][0] - origin[0]) * (points[i + 1][1] - origin[1])
                - (points[i][1] - origin[1]) * (points[i + 1][0] - origin[0])
                for i in range(1, len(points) - 1)
            )
        )
        / 2
    )


def _clip(points, axis, edge, keep_greater):
    if not points:
        return []
    result = []
    previous = points[-1]
    inside_previous = previous[axis] >= edge if keep_greater else previous[axis] <= edge
    for current in points:
        inside = current[axis] >= edge if keep_greater else current[axis] <= edge
        if inside != inside_previous:
            ratio = (edge - previous[axis]) / (current[axis] - previous[axis])
            other = 1 - axis
            point = [0.0, 0.0]
            point[axis] = edge
            point[other] = previous[other] + ratio * (current[other] - previous[other])
            result.append(tuple(point))
        if inside:
            result.append(current)
        previous, inside_previous = current, inside
    return result


def intersection_area(points, bounds):
    clipped = points
    for axis, edge, greater in (
        (0, bounds[0], True),
        (0, bounds[2], False),
        (1, bounds[1], True),
        (1, bounds[3], False),
    ):
        clipped = _clip(clipped, axis, edge, greater)
    return area(clipped)


def select_bottle(quad, image_id, detections):
    """Center inside, >=80% actual quad area, unique smallest bottle area; else None.

    The center is the arithmetic mean of four serialized vertices, not a bounding-box
    center. No score/ID tie break, cross-image matching, rounding or epsilon widening.
    """
    if len(detections) > MAX_DETECTIONS:
        raise ValueError("Association detection capacity exceeded")
    points = polygon(quad)
    text_area = area(points)
    if text_area <= 0:
        raise ValueError("Text quad must have positive area")
    cx, cy = (math.fsum(p[axis] for p in points) / 4 for axis in (0, 1))
    candidates = []
    seen = set()
    for detection in detections:
        bounds = box(detection["bbox"])
        key = detection["detection_id"]
        if key in seen:
            raise ValueError("Duplicate detection identity")
        seen.add(key)
        if detection["image_id"] != image_id or detection["type"] != "bottle":
            continue
        if detection["class_id"] != BOTTLE_CLASS_ID:
            raise ValueError("Bottle class mismatch")
        x1, y1, x2, y2 = bounds
        if (
            x1 <= cx <= x2
            and y1 <= cy <= y2
            and (intersection_area(points, bounds) >= 0.8 * text_area)
        ):
            candidates.append(((x2 - x1) * (y2 - y1), key))
    if not candidates:
        return None
    smallest = min(size for size, _ in candidates)
    winners = [key for size, key in candidates if size == smallest]
    return winners[0] if len(winners) == 1 else None


def associate_rows(rows, detections):
    """Mutate only nullable association; keep line/crop IDs, pixels and OCR unchanged."""
    if len(rows) > MAX_REGIONS or len(detections) > MAX_DETECTIONS:
        raise ValueError("Association capacity exceeded")
    # Compute all before mutating, so malformed geometry cannot leave partial associations.
    associations = [
        select_bottle(row["crop_evidence"]["recipe"]["quad"], row["image_id"], detections)
        for row in rows
    ]
    for row, association in zip(rows, associations):
        row["parent_detection_id"] = association
