"""Real-model adapters; they never perform business writes."""

from .dfine import (
    ADAPTER_ID,
    CLASS_COUNT,
    CLASSES,
    OFFICIAL_CONFIG_SHA256,
    OFFICIAL_MODEL_SHA256,
    OFFICIAL_PREPROCESSOR_SHA256,
    AdapterError,
    ArtifactEvidence,
    OnnxDetector,
    adapt_outputs,
    inspect_artifacts,
    postprocess_project,
    preprocess_rgb,
    require_activation,
    validate_session_contract,
)

__all__ = [
    "ADAPTER_ID",
    "AdapterError",
    "ArtifactEvidence",
    "CLASS_COUNT",
    "CLASSES",
    "OFFICIAL_CONFIG_SHA256",
    "OFFICIAL_MODEL_SHA256",
    "OFFICIAL_PREPROCESSOR_SHA256",
    "OnnxDetector",
    "adapt_outputs",
    "inspect_artifacts",
    "postprocess_project",
    "preprocess_rgb",
    "require_activation",
    "validate_session_contract",
]
