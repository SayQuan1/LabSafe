"""Pure D-FINE contract reference; no image library, model, GPU or business writes."""
import math

ADAPTER_ID = 'dfine-n4-rgb-stretch-v1'
SOURCE_COMMIT = '956d1709314c2c6a4df6f34de232054578a7449f'
CLASSES = ('bottle', 'label', 'shelf', 'cabinet')
CATEGORY_TO_CLASS = {1: 0, 2: 1, 3: 2, 4: 3}
CLASS_TO_CATEGORY = {value: key for key, value in CATEGORY_TO_CLASS.items()}
QUERY_COUNT = 300
MAX_DETECTIONS = 100

def require(ok, message):
    if not ok:
        raise ValueError(message)

def category_to_class(category_id):
    require(type(category_id) is int and category_id in CATEGORY_TO_CLASS, 'invalid COCO category')
    return CATEGORY_TO_CLASS[category_id]

def class_to_category(class_id):
    require(type(class_id) is int and class_id in CLASS_TO_CATEGORY, 'invalid model class')
    return CLASS_TO_CATEGORY[class_id]

def finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def image_size(size):
    require(len(size) == 2 and all(type(x) is int and x > 0 for x in size), 'size must be [width,height]')
    return size

def rgb_pixel_to_float(pixel):
    require(len(pixel) == 3 and all(type(x) is int and 0 <= x <= 255 for x in pixel), 'invalid RGB8 pixel')
    return [x / 255.0 for x in pixel]

def query_outputs(logits, boxes_cxcywh, original_size):
    """Mirror project export wrapper: qmax sigmoid, no TopK/NMS; query order retained.

    Inputs are unbatched [300,4] normalized model boxes and logits. Export uses
    float32 tensors; this scalar float64 reference specifies semantics, not GPU tolerance.
    """
    width, height = image_size(original_size)
    require(len(logits) == len(boxes_cxcywh) == QUERY_COUNT, 'SCHEMA_MISMATCH: query count')
    labels, boxes, scores = [], [], []
    for row, box in zip(logits, boxes_cxcywh):
        require(len(row) == len(box) == 4, 'SCHEMA_MISMATCH: four classes/box coordinates')
        require(all(finite(x) for x in row + box), 'MODEL_ERROR: nonfinite output')
        label = max(range(4), key=lambda i: (row[i], -i))
        value = row[label]
        score = 1 / (1 + math.exp(-value)) if value >= 0 else math.exp(value) / (1 + math.exp(value))
        cx, cy, bw, bh = box
        require(bw > 0 and bh > 0, 'MODEL_ERROR: nonpositive box')
        labels.append(label)
        scores.append(score)
        boxes.append([(cx-bw/2)*width, (cy-bh/2)*height, (cx+bw/2)*width, (cy+bh/2)*height])
    return labels, boxes, scores

def adapt_outputs(labels, boxes_xyxy, scores, original_size, detection_min):
    """Validate all candidates before filtering; return image-normalized boxes."""
    width, height = image_size(original_size)
    require(finite(detection_min) and 0 <= detection_min <= 1, 'invalid threshold')
    require(len(labels) == len(boxes_xyxy) == len(scores) == QUERY_COUNT, 'SCHEMA_MISMATCH: output shape')
    result = []
    for query_index, (label, box, score) in enumerate(zip(labels, boxes_xyxy, scores)):
        class_to_category(label)
        require(len(box) == 4, 'SCHEMA_MISMATCH: box shape')
        require(all(finite(x) for x in box) and finite(score), 'MODEL_ERROR: nonfinite output')
        require(0 <= score <= 1 and box[2] > box[0] and box[3] > box[1], 'MODEL_ERROR: score/box')
        if score < detection_min:
            continue
        x1, y1, x2, y2 = box
        clipped = [max(0, min(width, x1))/width, max(0, min(height, y1))/height,
                   max(0, min(width, x2))/width, max(0, min(height, y2))/height]
        if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
            continue
        result.append({'class_id': label, 'type': CLASSES[label], 'bbox': clipped,
                       'confidence': score, 'query_index': query_index})
    result.sort(key=lambda row: (row['bbox'][1], row['bbox'][0], row['bbox'][3], row['bbox'][2],
                                 row['class_id'], row['confidence'], row['query_index']))
    return result

def check_request_limit(image_results):
    require(1 <= len(image_results) <= 3, 'invalid image count')
    require(sum(map(len, image_results)) <= MAX_DETECTIONS, 'MODEL_ERROR: detection capacity exceeded')
    return True
