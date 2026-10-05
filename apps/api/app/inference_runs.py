"""Inspection item submission route."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from apps.api.app.foundations import Identifier
from apps.api.app.identity import Body, CanonicalJSONResponse
from packages.application.inference_runs import InferenceRunApplication
from packages.domain.security import ServiceError
from packages.persistence.security import SESSION_COOKIE


class ImageSelectionBody(Body):
    image_id: Identifier
    role: Literal["overview", "detail"]
    parent_image_id: Identifier | None


class ItemSubmitBody(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    images: list[ImageSelectionBody] = Field(min_length=1, max_length=3)


def mount_inference_runs(app, identity):
    app.state.inference_runs = InferenceRunApplication(identity) if identity is not None else None
    router = APIRouter(prefix="/api/v1")

    @router.post(
        "/inspection-items/{item_id}/submit", status_code=202, operation_id="submitInspectionItem"
    )
    def submit_item(item_id: UUID, body: ItemSubmitBody, request: Request):
        if request.query_params:
            raise ServiceError("VALIDATION_ERROR", 422, "Submit takes no query parameters")
        if app.state.inference_runs is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Inference service not configured")
        value = body.model_dump(mode="json")
        status, response = app.state.inference_runs.submit(
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            str(item_id),
            value,
            request.state.request_id,
        )
        return CanonicalJSONResponse(response, status_code=status)

    app.include_router(router)
