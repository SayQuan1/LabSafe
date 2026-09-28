import json
import math
import unittest
from importlib.resources import files

from packages.inference_protocol.contract import DOCUMENT, InferenceRequest, validate
from packages.inference_protocol.hashing import canonical_json, request_hash


class ContractTests(unittest.TestCase):
    def test_canonical_serialization(self):
        self.assertEqual(canonical_json({"b": 1, "a": [True]}), '{"a":[true],"b":1}')
        self.assertEqual(canonical_json({"x": "中文"}), '{"x":"中文"}')
        with self.assertRaises(ValueError):
            canonical_json({"x": math.nan})

    def test_retry_identity_excludes_attempt_fencing_and_deadline(self):
        from packages.inference_protocol.hashing import REQUEST_HASH_FIELDS

        payload = {key: key for key in REQUEST_HASH_FIELDS}
        original = request_hash(payload)
        payload.update(attempt_id="retry", fencing_token=9, deadline_at="later")
        self.assertEqual(original, request_hash(payload))
        payload["submission_revision"] = "new"
        self.assertNotEqual(original, request_hash(payload))

    def test_generated_package_is_available_as_a_resource(self):
        content = files("packages.inference_protocol").joinpath("contract.json").read_text("utf-8")
        self.assertEqual(json.loads(content), DOCUMENT)
        self.assertEqual(DOCUMENT["servers"][0]["url"], "/internal/inference/v1")
        self.assertEqual(
            set(DOCUMENT["paths"]), {"/health", "/ready", "/version", "/quality", "/runs"}
        )

    def test_empty_request_is_not_a_dto(self):
        with self.assertRaises(ValueError):
            InferenceRequest.model_validate({})
        with self.assertRaises(ValueError):
            validate("InferenceResult", {"facts": []})
