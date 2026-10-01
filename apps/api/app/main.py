"""Development-only business API with opt-in upload validation acceptance."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from apps.api.app.foundations import mount_foundations
from apps.api.app.identity import configured_identity, mount_identity
from apps.api.app.item_queries import mount_item_queries
from apps.api.app.uploads import mount_uploads
from packages.shared.environment import development_environment


def create_app(*, identity=None, public_origin=None, storage=None) -> FastAPI:
    environment = development_environment()
    enabled = os.getenv("API_IDENTITY_ENABLED", "0")
    if enabled not in {"0", "1"}:
        raise ValueError("API_IDENTITY_ENABLED must be 0 or 1")
    upload_enabled = os.getenv("API_UPLOADS_ENABLED", "0")
    if upload_enabled not in {"0", "1"}:
        raise ValueError("API_UPLOADS_ENABLED must be 0 or 1")
    owned = identity is None and enabled == "1"
    if owned:
        identity, public_origin = configured_identity(environment)
    owns_storage = upload_enabled == "1" and storage is None
    try:
        if upload_enabled == "1" and identity is None:
            raise ValueError("Upload grants require configured identity")
        if owns_storage:
            from packages.storage.s3 import S3Settings, S3Storage

            storage = S3Storage(S3Settings.from_environment(public_origin))
    except Exception:
        if owned:
            identity.engine.dispose()
            identity.limits.redis.close()
        raise

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            try:
                if owns_storage:
                    storage.close()
            finally:
                if owned:
                    identity.engine.dispose()
                    identity.limits.redis.close()

    app = FastAPI(title="LabSafe API", version="0.1.0-dev", lifespan=lifespan)
    mount_identity(app, identity, public_origin)
    mount_foundations(app, identity)
    mount_item_queries(app, identity)
    mount_uploads(app, identity, storage)

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        return {"status": "alive", "environment": environment, "is_simulated": True}

    @app.get("/ready")
    def ready() -> dict[str, str | bool]:
        if app.state.identity is not None:
            app.state.identity.readiness()
            return {
                "status": "ready",
                "environment": environment,
                "is_simulated": True,
                "scope": (
                    "I-02B/C identity, I-02D foundations, I-02E item queries "
                    "and optional I-02F1/F2 upload signing and validation acceptance only; "
                    "storage, workflow and real-model readiness not implemented"
                ),
            }
        return {
            "status": "ready",
            "environment": environment,
            "is_simulated": True,
            "scope": "I-01A process only; database and business readiness not implemented",
        }

    return app
