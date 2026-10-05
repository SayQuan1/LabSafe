"""Facts and finding review API adapters."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from apps.api.app.foundations import Identifier
from apps.api.app.identity import Body, CanonicalJSONResponse, VersionCommand, pagination
from packages.application.review import ReviewApplication
from packages.domain.security import ServiceError
from packages.persistence.security import SESSION_COOKIE


class EvidenceRefBody(Body):
    image_id: Identifier
    detection_id: Identifier | None
    crop_id: Identifier | None


class EntityFactBody(Body):
    detection_id: Identifier
    entity_id: Identifier | None
    resolution: Literal["resolved", "candidate", "unknown"]
    source: Literal["model", "human"]
    evidence: Annotated[list[EvidenceRefBody], Field(min_length=1, max_length=10)]


class RelationFactBody(Body):
    source_detection_id: Identifier
    target_detection_id: Identifier
    relation: Literal["adjacent", "not_adjacent", "unknown"]
    same_location: bool | None
    source: Literal["model", "human"]
    evidence: Annotated[list[EvidenceRefBody], Field(min_length=1, max_length=10)]


class DateFactBody(Body):
    detection_id: Identifier
    kind: Literal["expiry", "production", "opened", "unknown"]
    value: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None
    source: Literal["model", "human"]
    evidence: Annotated[list[EvidenceRefBody], Field(min_length=1, max_length=10)]


class FactsEditBody(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    entities: Annotated[list[EntityFactBody], Field(max_length=100)]
    relations: Annotated[list[RelationFactBody], Field(max_length=200)]
    dates: Annotated[list[DateFactBody], Field(max_length=100)]
    reason: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class FindingUncertainBody(VersionCommand):
    reason_code: Literal[
        "blur",
        "occluded",
        "label_unreadable",
        "rule_missing",
        "insufficient_evidence",
        "other",
    ]


def mount_review(app, identity):
    app.state.review = ReviewApplication(identity) if identity is not None else None
    router = APIRouter(prefix="/api/v1")

    def context(request, allowed=()):
        if any(
            key not in allowed or len(request.query_params.getlist(key)) != 1
            for key in request.query_params
        ):
            raise ServiceError("VALIDATION_ERROR", 422, "Unexpected or duplicate query parameter")
        if app.state.review is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Review service not configured")
        return app.state.review

    def read(request, kind, resource_id=None):
        allowed = (
            ()
            if resource_id
            else (
                "page",
                "page_size",
                "laboratory_id",
                "current_only",
                "status",
                "severity",
            )
        )
        service = context(request, allowed)
        page, size = pagination(request)
        laboratory_id = request.query_params.get("laboratory_id")
        if laboratory_id is not None:
            try:
                laboratory_id = str(UUID(laboratory_id))
            except (TypeError, ValueError):
                raise ServiceError("VALIDATION_ERROR", 422, "Invalid laboratory_id") from None
        raw_current = request.query_params.get("current_only", "true")
        if raw_current not in {"true", "false"}:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid current_only")
        status = request.query_params.get("status")
        if status is not None and status not in {
            "needs_review",
            "confirmed",
            "rejected",
            "cannot_determine",
            "dispatched",
            "closed",
        }:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid finding status")
        severity = request.query_params.get("severity")
        if severity is not None and severity not in {"low", "medium", "high", "critical"}:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid finding severity")
        return service.read(
            kind,
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            resource_id=str(resource_id) if resource_id else None,
            laboratory_id=laboratory_id,
            page=page,
            page_size=size,
            current_only=raw_current == "true",
            status=status,
            severity=severity,
        )

    def write(request, operation, resource_id, body):
        service = context(request)
        status, response = service.write(
            operation,
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            str(resource_id),
            body.model_dump(mode="json"),
            request.state.request_id,
        )
        return CanonicalJSONResponse(response, status_code=status)

    @router.get("/inspection-items/{item_id}/facts", operation_id="getCurrentFacts")
    def get_current_facts(item_id: UUID, request: Request):
        return read(request, "facts", item_id)

    @router.post(
        "/inspection-items/{item_id}/facts", status_code=202, operation_id="factsInspectionItem"
    )
    def edit_facts(item_id: UUID, body: FactsEditBody, request: Request):
        return write(request, "facts", item_id, body)

    @router.get("/findings", operation_id="listFindings")
    def list_findings(request: Request):
        return read(request, "findings")

    @router.get("/findings/{finding_id}", operation_id="getFinding")
    def get_finding(finding_id: UUID, request: Request):
        return read(request, "finding", finding_id)

    @router.post("/findings/{finding_id}/confirm", operation_id="confirmFinding")
    def confirm_finding(finding_id: UUID, body: VersionCommand, request: Request):
        return write(request, "confirm", finding_id, body)

    @router.post("/findings/{finding_id}/reject", operation_id="rejectFinding")
    def reject_finding(finding_id: UUID, body: VersionCommand, request: Request):
        return write(request, "reject", finding_id, body)

    @router.post("/findings/{finding_id}/cannot-determine", operation_id="cannotdetermineFinding")
    def cannot_determine(finding_id: UUID, body: FindingUncertainBody, request: Request):
        return write(request, "cannot-determine", finding_id, body)

    app.include_router(router)
