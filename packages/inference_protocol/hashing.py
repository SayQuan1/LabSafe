"""Canonical JSON and request identity shared by producer and consumer."""

import hashlib
import json
from typing import Any

REQUEST_HASH_FIELDS = (
    "tenant_id",
    "laboratory_id",
    "item_id",
    "run_id",
    "submission_revision",
    "model_bundle_id",
    "model_checksum",
    "dictionary_version_id",
    "dictionary_sha256",
    "pipeline_version",
    "device_profile",
    "image_refs",
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def request_hash(payload: dict[str, Any]) -> str:
    return sha256_json({name: payload[name] for name in REQUEST_HASH_FIELDS})
