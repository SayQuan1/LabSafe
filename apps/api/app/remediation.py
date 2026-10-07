"""Strict remediation task and evidence API adapters."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BeforeValidator, Field

from apps.api.app.foundations import Identifier
from apps.api.app.identity import Body, CanonicalJSONResponse, pagination
from packages.application.remediation import RemediationApplication
from packages.domain.security import ServiceError
from packages.persistence.security import SESSION_COOKIE


def _parse_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Invalid datetime") from error
    else:
        raise ValueError("Invalid datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Datetime must include timezone")
    return parsed


class RemediationCreate(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    assignee_id: Identifier
    due_at: Annotated[datetime, BeforeValidator(_parse_datetime)]
    priority: Literal["low", "medium", "high", "critical"]
    description: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]


class EvidenceSubmit(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    image_ids: Annotated[list[Identifier], Field(min_length=1, max_length=10)]
    description: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]


class Recheck(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    evidence_id: Identifier
    reason: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class TaskAssignment(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    assignee_id: Identifier
    due_at: Annotated[datetime, BeforeValidator(_parse_datetime)]
    reason: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class Reason(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    reason: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


def mount_remediation(app, identity):
    app.state.remediation = RemediationApplication(identity) if identity is not None else None
    router = APIRouter(prefix="/api/v1")

    def context(request, allowed=()):
        if any(
            k not in allowed or len(request.query_params.getlist(k)) != 1
            for k in request.query_params
        ):
            raise ServiceError("VALIDATION_ERROR", 422, "Unexpected or duplicate query parameter")
        if app.state.remediation is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Remediation service not configured")
        return app.state.remediation

    def read(request, kind, resource_id=None):
        allowed = (
            ()
            if resource_id
            else (
                "page",
                "page_size",
                "laboratory_id",
                "status",
                "overdue",
            )
        )
        if kind == "evidence":
            allowed = ("page", "page_size", "laboratory_id")
        service = context(request, allowed)
        page, size = pagination(request)
        lab = request.query_params.get("laboratory_id")
        if lab:
            try:
                lab = str(UUID(lab))
            except (TypeError, ValueError):
                raise ServiceError("VALIDATION_ERROR", 422, "Invalid laboratory_id") from None
        status = request.query_params.get("status")
        if status and status not in {
            "pending_dispatch",
            "in_progress",
            "pending_recheck",
            "rejected",
            "cannot_remediate",
            "closed",
        }:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid remediation status")
        overdue = request.query_params.get("overdue")
        if overdue is not None and overdue not in {"true", "false"}:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid overdue filter")
        return service.read(
            kind,
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            resource_id=str(resource_id) if resource_id else None,
            laboratory_id=lab,
            page=page,
            page_size=size,
            status=status,
            overdue=None if overdue is None else overdue == "true",
        )

    def write(request, operation, resource_id, body):
        status, response = context(request).write(
            operation,
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            str(resource_id),
            body.model_dump(mode="json"),
            request.state.request_id,
        )
        return CanonicalJSONResponse(response, status_code=status)

    @router.post(
        "/findings/{finding_id}/remediation-tasks",
        status_code=201,
        operation_id="dispatchRemediation",
    )
    def dispatch(finding_id: UUID, body: RemediationCreate, request: Request):
        return write(request, "dispatch", finding_id, body)

    @router.get("/remediation-tasks", operation_id="listRemediations")
    def tasks(request: Request):
        return read(request, "tasks")

    @router.get("/remediation-tasks/{task_id}", operation_id="getRemediation")
    def task(task_id: UUID, request: Request):
        return read(request, "task", task_id)

    @router.post("/remediation-tasks/{task_id}/accept", operation_id="acceptRemediation")
    def accept(task_id: UUID, body: Reason, request: Request):
        return write(request, "accept", task_id, body)

    @router.post(
        "/remediation-tasks/{task_id}/submit-evidence",
        operation_id="submitevidenceRemediation",
    )
    def evidence(task_id: UUID, body: EvidenceSubmit, request: Request):
        return write(request, "submit-evidence", task_id, body)

    @router.post("/remediation-tasks/{task_id}/recheck", operation_id="recheckRemediation")
    def recheck(task_id: UUID, body: Recheck, request: Request):
        return write(request, "recheck", task_id, body)

    @router.post("/remediation-tasks/{task_id}/reject", operation_id="rejectRemediation")
    def reject(task_id: UUID, body: Recheck, request: Request):
        return write(request, "reject", task_id, body)

    @router.post(
        "/remediation-tasks/{task_id}/cannot-remediate", operation_id="cannotremediateRemediation"
    )
    def cannot(task_id: UUID, body: Reason, request: Request):
        return write(request, "cannot-remediate", task_id, body)

    @router.post("/remediation-tasks/{task_id}/reassign", operation_id="reassignRemediation")
    def reassign(task_id: UUID, body: TaskAssignment, request: Request):
        return write(request, "reassign", task_id, body)

    @router.get("/remediation-tasks/{task_id}/evidence", operation_id="listEvidence")
    def evidence_list(task_id: UUID, request: Request):
        return read(request, "evidence", task_id)

    app.include_router(router)
