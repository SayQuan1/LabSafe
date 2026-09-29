"""Explicit, development-only AI startup settings and verified fixture identity."""

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class Settings:
    environment: str
    token: str = field(repr=False)
    allowed_tenants: frozenset[str]
    scenario: str
    delay_ms: int
    identity: dict[str, Any]
    model_checksum: str
    dictionary_sha256: str

    @classmethod
    def from_env(cls) -> "Settings":
        if sys.version_info[:2] != (3, 11):
            raise RuntimeError("LabSafe requires Python 3.11")
        environment = os.environ.get("APP_ENV")
        if environment not in {"dev", "test"}:
            raise RuntimeError("I-01A requires APP_ENV=dev or test; production is disabled")
        if os.environ.get("AI_MODE") != "mock":
            raise RuntimeError("I-01A implements AI_MODE=mock only; real inference is not ready")
        if os.environ.get("AI_MAX_INFLIGHT", "1") != "1":
            raise RuntimeError("AI_MAX_INFLIGHT must be 1")
        token_path = os.environ.get("AI_TOKEN_FILE")
        if not token_path:
            raise RuntimeError("AI_TOKEN_FILE is required")
        try:
            token = Path(token_path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("Cannot read AI_TOKEN_FILE") from exc
        if not token.isascii() or len(token) < 32 or any(c.isspace() for c in token):
            raise RuntimeError("AI_TOKEN_FILE must contain an ASCII token of at least 32 bytes")
        raw_tenants = os.environ.get("AI_ALLOWED_TENANTS", "")
        try:
            allowed_tenants = frozenset(str(UUID(v.strip())) for v in raw_tenants.split(","))
        except ValueError as exc:
            raise RuntimeError("AI_ALLOWED_TENANTS requires explicit tenant UUIDs") from exc
        scenario = os.environ.get("AI_FIXTURE_SCENARIO", "no_targets")
        if scenario not in {"no_targets", "needs_retake", "error", "timeout"}:
            raise RuntimeError("Unknown AI_FIXTURE_SCENARIO")
        try:
            delay_ms = int(os.environ.get("AI_FIXTURE_DELAY_MS", "0"))
        except ValueError as exc:
            raise RuntimeError("AI_FIXTURE_DELAY_MS must be an integer") from exc
        if not 0 <= delay_ms <= 10000:
            raise RuntimeError("AI_FIXTURE_DELAY_MS must be between 0 and 10000")
        commit = os.environ.get("SERVICE_COMMIT", "local-development")
        if not 1 <= len(commit) <= 40:
            raise RuntimeError("SERVICE_COMMIT must contain 1 to 40 characters")
        fixture_dir = files("apps.ai_inference").joinpath("fixtures")
        model_bytes = fixture_dir.joinpath("model.json").read_bytes()
        dictionary_bytes = fixture_dir.joinpath("dictionary.json").read_bytes()
        lock_bytes = fixture_dir.joinpath("runtime-lock.json").read_bytes()
        runtime_lock = json.loads(lock_bytes)
        for name, expected in runtime_lock["packages"].items():
            if version(name) != expected:
                raise RuntimeError(f"Fixture dependency mismatch: {name} must be {expected}")
        model, dictionary = json.loads(model_bytes), json.loads(dictionary_bytes)
        identity = {
            "service_commit": commit,
            "pipeline_version": "vision-v1",
            "model_bundle_id": model["model_bundle_id"],
            "dictionary_version_id": dictionary["dictionary_version_id"],
            "device_profile": "cpu",
            "purpose": "development",
            "is_simulated": True,
            "adapter_id": "fixture-v1",
            "runtime_profile": "fixture-v1",
            "runtime_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
            "detector_device": "mock",
            "ocr_device": "mock",
        }
        return cls(
            environment,
            token,
            allowed_tenants,
            scenario,
            delay_ms,
            identity,
            hashlib.sha256(model_bytes).hexdigest(),
            hashlib.sha256(dictionary_bytes).hexdigest(),
        )
