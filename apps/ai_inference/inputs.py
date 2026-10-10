"""Shared request hash, ordered image graph and canonical analysis references."""

from apps.ai_inference.adapters.dfine import AdapterError
from apps.ai_inference.analysis import verify_reference
from packages.inference_protocol.contract import validate
from packages.inference_protocol.hashing import request_hash


def verify_image_inputs(payload, allowed_tenants):
    try:
        validate("InferenceRequest", payload)
    except ValueError:
        raise AdapterError("VALIDATION_ERROR", "Invalid inference request") from None
    if payload["tenant_id"] not in allowed_tenants:
        raise AdapterError("FORBIDDEN", "Tenant is not allowed")
    if payload["request_hash"] != request_hash(payload):
        raise AdapterError("HASH_MISMATCH", "Request content hash mismatch")
    refs = payload["image_refs"]
    overview = refs[0]
    if overview["role"] != "overview" or overview["parent_image_id"] is not None:
        raise AdapterError("VALIDATION_ERROR", "First image must be the unique overview")
    if len({image["image_id"] for image in refs}) != len(refs):
        raise AdapterError("VALIDATION_ERROR", "Image IDs must be unique")
    for index, image in enumerate(refs):
        verify_reference(image, payload["tenant_id"], payload["laboratory_id"], allowed_tenants)
        if image["location_id"] != overview["location_id"]:
            raise AdapterError("VALIDATION_ERROR", "Images must share a location")
        if index and (
            image["role"] != "detail" or image["parent_image_id"] != overview["image_id"]
        ):
            raise AdapterError("VALIDATION_ERROR", "Detail must refer to the overview")
