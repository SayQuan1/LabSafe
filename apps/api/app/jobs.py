"""Strict job metadata and administrator replay adapters; validate_image branch only."""

from uuid import UUID

from fastapi import APIRouter, Request

from apps.api.app.identity import CanonicalJSONResponse, VersionCommand, pagination
from packages.application.jobs import JobApplication
from packages.domain.security import ServiceError
from packages.persistence.security import SESSION_COOKIE


def mount_jobs(app, identity, storage):
    app.state.jobs = JobApplication(identity, storage) if identity is not None else None
    router = APIRouter(prefix="/api/v1")

    def context(request, allowed=()):
        if any(
            k not in allowed or len(request.query_params.getlist(k)) != 1
            for k in request.query_params
        ):
            raise ServiceError("VALIDATION_ERROR", 422, "Unexpected or duplicate query parameter")
        if app.state.jobs is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Job service not configured")
        return app.state.jobs

    @router.get("/jobs/{job_id}", operation_id="getJob")
    def get_job(job_id: UUID, request: Request):
        return context(request).read(
            "get", request.cookies.get(SESSION_COOKIE), request.state.request_id, job_id=str(job_id)
        )

    @router.get("/dead-letters", operation_id="listDeadLetters")
    def dead_letters(request: Request, laboratory_id: UUID | None = None):
        service = context(request, ("page", "page_size", "laboratory_id"))
        page, size = pagination(request)
        return service.read(
            "dead_letters",
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            page=page,
            page_size=size,
            laboratory_id=str(laboratory_id) if laboratory_id else None,
        )

    @router.post("/dead-letters/{job_id}/replay", status_code=202, operation_id="replayJob")
    def replay(job_id: UUID, body: VersionCommand, request: Request):
        status, response = context(request).replay(
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            str(job_id),
            body.model_dump(),
            request.state.request_id,
        )
        return CanonicalJSONResponse(response, status_code=status)

    app.include_router(router)
