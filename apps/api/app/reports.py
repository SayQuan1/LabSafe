"""Strict report snapshot creation and status query adapters."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field, field_validator

from apps.api.app.foundations import Identifier
from apps.api.app.identity import Body, CanonicalJSONResponse
from packages.application.reports import ReportApplication
from packages.domain.security import ServiceError
from packages.domain.uploads import capture_time
from packages.persistence.security import SESSION_COOKIE


class ExportFilter(Body):
    laboratory_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]
    from_: str = Field(alias="from")
    to: str
    severity: Annotated[list[Literal["low", "medium", "high", "critical"]], Field(max_length=4)]
    finding_status: Annotated[
        list[
            Literal[
                "needs_review", "confirmed", "rejected", "cannot_determine", "dispatched", "closed"
            ]
        ],
        Field(max_length=6),
    ]

    @field_validator("from_", "to")
    @classmethod
    def date_time(cls, value):
        capture_time(value)
        return value


class ExportCreate(Body):
    format: Literal["csv", "pdf"]
    filters: ExportFilter


def mount_reports(app, identity, storage=None):
    app.state.reports = ReportApplication(identity, storage) if identity is not None else None
    router = APIRouter(prefix="/api/v1")

    def service(request):
        if request.query_params:
            raise ServiceError("VALIDATION_ERROR", 422, "Report endpoints take no query parameters")
        if app.state.reports is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Report service not configured")
        return app.state.reports

    def download_service(request):
        if request.query_params:
            raise ServiceError("VALIDATION_ERROR", 422, "Report endpoints take no query parameters")
        report_service = service(request)
        if report_service.storage is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Report downloads not configured")
        return report_service

    @router.post("/reports/exports", status_code=202, operation_id="createExport")
    def create(body: ExportCreate, request: Request):
        status, response = service(request).create(
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            body.model_dump(mode="json", by_alias=True),
            request.state.request_id,
        )
        return CanonicalJSONResponse(response, status_code=status)

    @router.get("/reports/exports/{export_id}", operation_id="getExport")
    def get(export_id: UUID, request: Request):
        return service(request).read(
            request.cookies.get(SESSION_COOKIE), request.state.request_id, str(export_id)
        )

    @router.get("/reports/exports/{export_id}/download", operation_id="downloadExport")
    def download(export_id: UUID, request: Request):
        return CanonicalJSONResponse(
            download_service(request).download(
                request.cookies.get(SESSION_COOKIE), str(export_id), request.state.request_id
            ),
            headers={"Referrer-Policy": "no-referrer"},
        )

    app.include_router(router)
