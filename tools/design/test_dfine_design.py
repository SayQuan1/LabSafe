"""Synthetic D-FINE semantic tests; not trained-model, image-resize or GPU tests."""
import copy
from dfine_reference import (COCO_CATEGORY_IDS, category_to_class, class_to_category,
                             rgb_pixel_to_float, query_outputs, adapt_outputs, check_request_limit)

def run_tests():
    count = 0
    def ok(value):
        nonlocal count
        assert value
        count += 1
    def rejected(call):
        nonlocal count
        try:
            call()
        except ValueError:
            count += 1
        else:
            raise AssertionError('invalid D-FINE input accepted')
    for category in COCO_CATEGORY_IDS:
        ok(class_to_category(category_to_class(category)) == category)
    for invalid in (0, 12, 91, True, '1'):
        rejected(lambda value=invalid: category_to_class(value))
    for invalid in (-1, 80, False, '0'):
        rejected(lambda value=invalid: class_to_category(value))
    ok(rgb_pixel_to_float([255, 0, 128]) == [1, 0, 128/255])
    rejected(lambda: rgb_pixel_to_float([256, 0, 0]))
    logits = [[-20.0]*80 for _ in range(300)]
    boxes = [[.5, .5, .5, .5] for _ in range(300)]
    logits[0][39] = 5
    logits[0][40] = 5
    logits[1] = [0]*80
    labels, xyxy, scores = query_outputs(logits, boxes, [1600, 800])
    ok(labels[0] == 39 and labels[1] == 0)
    ok(xyxy[0] == [400, 200, 1200, 600])
    ok(scores[1] == .5 and len(scores) == 300)
    rows = adapt_outputs(labels, xyxy, scores, [1600, 800], .5)
    ok(len(rows) == 2 and rows[0]['type'] == 'person' and rows[1]['type'] == 'bottle')
    ok(rows[0]['bbox'] == [.25, .25, .75, .75])
    ok(len(adapt_outputs(labels, xyxy, scores, [1600, 800], .500001)) == 1)
    ok(query_outputs(logits, boxes, [800, 1600])[1][0] == [200, 400, 600, 1200])
    clipped = copy.deepcopy(xyxy); clipped[0] = [-100, -50, 1700, 900]
    ok(adapt_outputs(labels, clipped, scores, [1600, 800], .9)[0]['bbox'] == [0, 0, 1, 1])
    outside = copy.deepcopy(xyxy); outside[0] = [-10, -10, -1, -1]
    ok(adapt_outputs(labels, outside, scores, [1600, 800], .9) == [])
    rejected(lambda: query_outputs(logits[:-1], boxes, [1600, 800]))
    bad = copy.deepcopy(logits); bad[0][2] = float('nan')
    rejected(lambda: query_outputs(bad, boxes, [1600, 800]))
    bad_box = copy.deepcopy(boxes); bad_box[0][2] = 0
    bad_labels, bad_xyxy, bad_scores = query_outputs(logits, bad_box, [1600, 800])
    rejected(lambda: adapt_outputs(bad_labels, bad_xyxy, bad_scores, [1600, 800], .5))
    rejected(lambda: query_outputs(logits, boxes, [0, 800]))
    bad_scores = scores.copy(); bad_scores[299] = float('inf')
    rejected(lambda: adapt_outputs(labels, xyxy, bad_scores, [1600, 800], .9))
    rejected(lambda: adapt_outputs(labels, xyxy, scores, [1600, 800], -.1))
    rejected(lambda: adapt_outputs(labels, xyxy[:-1], scores, [1600, 800], .9))
    bad_labels = labels.copy(); bad_labels[299] = 80
    rejected(lambda: adapt_outputs(bad_labels, xyxy, scores, [1600, 800], .9))
    ok(check_request_limit([[{}]*50, [{}]*50]))
    rejected(lambda: check_request_limit([[{}]*50, [{}]*51]))
    rejected(lambda: check_request_limit([]))
    # No NMS: distinct queries at identical coordinates are not silently merged.
    duplicate_logits = [[-20.0]*80 for _ in range(300)]
    duplicate_logits[0] = duplicate_logits[1] = [5] + [0]*79
    dl, db, ds = query_outputs(duplicate_logits, boxes, [1600, 800])
    ok(len(adapt_outputs(dl, db, ds, [1600, 800], .9)) == 2)
    return count

if __name__ == '__main__':
    print('Synthetic D-FINE semantic checks:', run_tests())
