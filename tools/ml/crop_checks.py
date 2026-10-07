"""Parent-side reconstruction of real child-process OCR crop evidence."""

import hashlib

from packages.image_evidence.perspective import rebuild_ocr_evidence


def verify_crops(report, paths):
    import numpy as np
    from PIL import Image

    checked, rotated, crop_ids = [], 0, set()
    if len(report["images"]) != len(paths):
        raise RuntimeError("Image count differs")
    for row, path in zip(report["images"], paths):
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["input_sha256"]:
            raise RuntimeError("Source file digest differs")
        # Smoke inputs are normalized, metadata-free RGB PNGs, like analysis images.
        with Image.open(path) as image:
            rgb = np.asarray(image.convert("RGB"), dtype=np.uint8)
        for line in row["lines"]:
            metadata = line["crop_evidence"]
            if (
                metadata["image_id"] != row["image_id"]
                or metadata["line_id"] != line["line_id"]
                or metadata["crop_id"] in crop_ids
                or [[p["x"], p["y"]] for p in metadata["recipe"]["quad"]] != line["quad"]
            ):
                raise RuntimeError("Crop references or source quad differ")
            crop_ids.add(metadata["crop_id"])
            evidence = rebuild_ocr_evidence(rgb, metadata)
            rotated += metadata["recognition_rotation_ccw"] == 90
            checked.append(
                {
                    "crop_id": metadata["crop_id"],
                    "png_sha256": evidence.sha256,
                    "png_size_bytes": len(evidence.png),
                    "recognition_rgb_sha256": metadata["recognition_rgb_sha256"],
                    "recognition_rotation_ccw": metadata["recognition_rotation_ccw"],
                }
            )
    return {
        "status": "PASS",
        "count": len(checked),
        "rotated_count": rotated,
        "source_hash_checked": True,
        "png_hash_checked": True,
        "recognition_pixels_checked": True,
        "crops": checked,
    }
