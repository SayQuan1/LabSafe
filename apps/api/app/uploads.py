"""Opt-in upload grants, validation acceptance, and read-only image metadata."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from apps.api.app.foundations import Identifier
from apps.api.app.identity import Body, CanonicalJSONResponse
from packages.application.uploads import UploadGrantApplication
from packages.domain.security import ServiceError
from packages.domain.uploads import MAX_BYTES
from packages.persistence.security import SESSION_COOKIE


class UploadRequest(Body):
    owner_type: Literal["inspection_item", "remediation_task"]
    owner_id: Identifier
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    size_bytes: Annotated[int, Field(ge=1, le=MAX_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    captured_at: str


class UploadComplete(Body):
    upload_id: Identifier
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def mount_uploads(app, identity, storage):
    app.state.uploads = (
        UploadGrantApplication(identity, storage)
        if identity is not None and storage is not None
        else None
    )
    router = APIRouter(prefix="/api/v1")

    def service(request):
        if request.query_params:
            raise ServiceError("VALIDATION_ERROR", 422, "Upload endpoints take no query parameters")
        if app.state.uploads is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Uploads not configured")
        return app.state.uploads

    @router.post("/uploads", status_code=201, operation_id="createUpload")
    def create(body: UploadRequest, request: Request):
        status, response = service(request).create(
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            body.model_dump(),
            request.state.request_id,
        )
        return CanonicalJSONResponse(response, status_code=status)

    @router.post("/uploads/complete", status_code=202, operation_id="completeUpload")
    def complete(body: UploadComplete, request: Request):
        status, response = service(request).complete(
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            body.model_dump(),
            request.state.request_id,
        )
        return CanonicalJSONResponse(response, status_code=status)

    @router.get("/images/{image_id}", operation_id="getImage")
    def image(image_id: UUID, request: Request):
        return service(request).get_image(
            request.cookies.get(SESSION_COOKIE), str(image_id), request.state.request_id
        )

    app.include_router(router)
