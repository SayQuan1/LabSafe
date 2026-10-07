import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from apps.ai_inference.adapters.dfine import (
    ADAPTER_ID,
    CLASS_COUNT,
    CLASSES,
    QUERY_COUNT,
    AdapterError,
    OnnxDetector,
    adapt_outputs,
    inspect_artifacts,
    postprocess_project,
    require_activation,
    validate_session_contract,
)


class _Value:
    def __init__(self, name, shape, dtype):
        self.name, self.shape, self.type = name, shape, dtype


class _Session:
    def get_inputs(self):
        return [
            _Value("images", (1, 3, 640, 640), "tensor(float)"),
            _Value("orig_target_sizes", (1, 2), "tensor(int64)"),
        ]

    def get_outputs(self):
        return [
            _Value("labels", (1, 300), "tensor(int64)"),
            _Value("boxes", (1, 300, 4), "tensor(float)"),
            _Value("scores", (1, 300), "tensor(float)"),
        ]


class _Array:
    def __init__(self, data, shape, dtype="float32"):
        self.data, self.shape, self.dtype = data, shape, dtype

    def tolist(self):
        return self.data


class DfineAdapterTests(unittest.TestCase):
    def test_official_metadata_is_activation_eligible_and_keeps_80_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.onnx").write_bytes(b"onnx-placeholder")
            (root / "config.json").write_text(
                json.dumps(
                    {
                        "model_type": "d_fine",
                        "num_queries": 300,
                        "id2label": {str(i): label for i, label in enumerate(CLASSES)},
                    }
                ),
                encoding="utf-8",
            )
            (root / "preprocessor_config.json").write_text(
                json.dumps(
                    {
                        "size": {"height": 640, "width": 640},
                        "do_resize": True,
                        "do_rescale": True,
                        "do_pad": False,
                        "do_normalize": False,
                        "rescale_factor": 1 / 255,
                    }
                ),
                encoding="utf-8",
            )
            evidence = inspect_artifacts(
                root / "model.onnx", root / "config.json", root / "preprocessor_config.json"
            )
            require_activation(evidence)
            self.assertTrue(evidence.activation_allowed)
            self.assertEqual(ADAPTER_ID, "dfine-coco80-rgb-stretch-v1")

    def test_postprocess_and_adapt_accept_class_79_and_reject_80(self):
        logits = [[-20.0] * CLASS_COUNT for _ in range(QUERY_COUNT)]
        logits[0][79] = 8.0
        boxes = [[0.5, 0.5, 0.5, 0.5] for _ in range(QUERY_COUNT)]
        rows = postprocess_project([logits], [boxes], [100, 200], 0.5)
        self.assertEqual(rows[0]["class_id"], 79)
        self.assertEqual(rows[0]["type"], CLASSES[79])
        labels = [79] + [0] * (QUERY_COUNT - 1)
        scores = [0.9] + [0.0] * (QUERY_COUNT - 1)
        boxes_xyxy = [[0, 0, 100, 200] for _ in range(QUERY_COUNT)]
        self.assertEqual(
            adapt_outputs([labels], [boxes_xyxy], [scores], [100, 200], 0.5)[0]["class_id"], 79
        )
        labels[-1] = 80
        with self.assertRaises(AdapterError) as error:
            adapt_outputs([labels], [boxes_xyxy], [scores], [100, 200], 0.5)
        self.assertEqual(error.exception.code, "MODEL_ERROR")

    def test_session_signature_is_fail_closed(self):
        validate_session_contract(_Session())
        session = _Session()
        session.get_inputs = lambda: [
            _Value("images", (1, 3, 320, 320), "tensor(float)"),
            _Value("orig_target_sizes", (1, 2), "tensor(int64)"),
        ]
        with self.assertRaises(AdapterError):
            validate_session_contract(session)

    def test_raw_export_dispatch_and_signature(self):
        session = _Session()
        session.get_inputs = lambda: [
            _Value("pixel_values", ("batch_size", 3, "height", "width"), "tensor(float)")
        ]
        session.get_outputs = lambda: [
            _Value("logits", ("batch_size", 300, 80), "tensor(float)"),
            _Value("pred_boxes", ("batch_size", 300, 4), "tensor(float)"),
        ]
        self.assertEqual(validate_session_contract(session), "raw")
        logits = [[-20.0] * 80 for _ in range(300)]
        logits[0][39] = 8.0

        def run(names, feed):
            self.assertEqual(names, ["logits", "pred_boxes"])
            self.assertEqual(feed, {"pixel_values": "tensor"})
            return [
                _Array([logits], (1, 300, 80)),
                _Array([[[0.5, 0.5, 0.5, 0.5]] * 300], (1, 300, 4)),
            ]

        session.run = run
        with patch(
            "apps.ai_inference.adapters.dfine.preprocess_rgb", return_value=("tensor", None)
        ):
            result = OnnxDetector(session).detect(SimpleNamespace(size=(800, 400)))
        self.assertEqual(result[0]["type"], "bottle")
        self.assertEqual(result[0]["bbox"], [0.25, 0.25, 0.75, 0.75])
        session.get_inputs = lambda: [_Value("pixel_values", (2, 3, 640, 640), "tensor(float)")]
        with self.assertRaises(AdapterError):
            validate_session_contract(session)

    def test_low_score_degenerate_box_is_filtered_but_nonfinite_and_retained_invalid_fail(self):
        logits = [[-20.0] * 80 for _ in range(300)]
        boxes = [[0.5, 0.5, 0.5, 0.5] for _ in range(300)]
        boxes[11] = [0.1, -0.05, 0.06, -0.005]
        diagnostics = {}
        self.assertEqual(
            postprocess_project([logits], [boxes], [800, 400], 0.4, diagnostics=diagnostics),
            [],
        )
        self.assertEqual(diagnostics["below_threshold"], 300)
        boxes[11][0] = float("nan")
        with self.assertRaises(AdapterError):
            postprocess_project([logits], [boxes], [800, 400], 0.4)
        boxes[11][0] = 0.1
        logits[11][39] = 8.0
        with self.assertRaises(AdapterError):
            postprocess_project([logits], [boxes], [800, 400], 0.4)

    def test_postprocessed_threshold_geometry_matches_raw_contract(self):
        labels = [[39] * 300]
        boxes = [[[0, 0, 1, -1]] * 300]
        scores = [[0.01] * 300]
        self.assertEqual(adapt_outputs(labels, boxes, scores, [800, 400], 0.4), [])
        scores[0][0] = 0.4
        with self.assertRaises(AdapterError):
            adapt_outputs(labels, boxes, scores, [800, 400], 0.4)

    def test_runtime_tensor_dtype_is_checked_before_casting(self):
        session = _Session()
        session.run = lambda *args: [
            _Array([[0] * 300], (1, 300), "float32"),
            _Array([[[0, 0, 10, 10]] * 300], (1, 300, 4)),
            _Array([[0.01] * 300], (1, 300)),
        ]
        with patch("apps.ai_inference.adapters.dfine.preprocess_rgb", return_value=(None, None)):
            with self.assertRaises(AdapterError) as error:
                OnnxDetector(session).detect(SimpleNamespace(size=(800, 400)))
        self.assertEqual(error.exception.code, "SCHEMA_MISMATCH")


if __name__ == "__main__":
    unittest.main()
