"""Strict public organization, template and inspection-draft adapters."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import AfterValidator, Field

from apps.api.app.identity import Body, CanonicalJSONResponse, VersionCommand, pagination
from packages.application.foundations import FoundationApplication
from packages.domain.security import ServiceError
from packages.persistence.security import SESSION_COOKIE


def canonical_uuid(value):
    parsed = str(UUID(value))
    if len(value) != 36 or parsed != value.lower():
        raise ValueError("Invalid UUID")
    return parsed


Identifier = Annotated[str, AfterValidator(canonical_uuid)]
Name = Annotated[str, Field(min_length=1, max_length=200)]
Code = Annotated[str, Field(min_length=1, max_length=64)]


class CollegeCreate(Body):
    name: Name
    code: Code


class LaboratoryCreate(CollegeCreate):
    college_id: Identifier


class LocationCreate(Body):
    parent_id: Identifier | None
    type: Literal["room", "area", "shelf", "cabinet"]
    label: Name


class TemplateItem(Body):
    id: Identifier
    code: Code
    title: Name
    capture_hint: Annotated[str, Field(min_length=1, max_length=1000)]
    sort_order: Annotated[int, Field(ge=0, le=2147483647)]
    required: bool


class TemplateCreate(Body):
    name: Name
    items: Annotated[list[TemplateItem], Field(min_length=1, max_length=100)]


class InspectionCreate(Body):
    laboratory_id: Identifier
    template_id: Identifier
    location_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]


def mount_foundations(app, identity):
    app.state.foundations = FoundationApplication(identity) if identity is not None else None
    router = APIRouter(prefix="/api/v1")

    def context(request, allowed=()):
        if any(
            key not in allowed or len(request.query_params.getlist(key)) != 1
            for key in request.query_params
        ):
            raise ServiceError("VALIDATION_ERROR", 422, "Unexpected or duplicate query parameter")
        if app.state.foundations is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Foundation service not configured")
        return app.state.foundations

    def read(request, kind, resource_id=None, laboratory_id=None, *, scoped=False):
        allowed = () if resource_id else ("page", "page_size")
        if scoped and resource_id is None:
            allowed += ("laboratory_id",)
        service = context(request, allowed)
        page, size = pagination(request)
        return service.read(
            kind,
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            resource_id=str(resource_id) if resource_id else None,
            laboratory_id=str(laboratory_id) if laboratory_id else None,
            page=page,
            page_size=size,
        )

    def write(request, kind, body, *, operation="create", resource_id=None, laboratory_id=None):
        status, response = context(request).write(
            kind,
            operation,
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            body.model_dump(),
            request.state.request_id,
            resource_id=str(resource_id) if resource_id else None,
            laboratory_id=str(laboratory_id) if laboratory_id else None,
        )
        return CanonicalJSONResponse(response, status_code=status)

    @router.get("/colleges", operation_id="listColleges")
    def colleges(request: Request):
        return read(request, "college")

    @router.get("/colleges/{resource_id}", operation_id="getCollege")
    def college(resource_id: UUID, request: Request):
        return read(request, "college", resource_id)

    @router.post("/colleges", status_code=201, operation_id="createCollege")
    def create_college(body: CollegeCreate, request: Request):
        return write(request, "college", body)

    @router.get("/laboratories", operation_id="listLaboratories")
    def laboratories(request: Request, laboratory_id: UUID | None = None):
        return read(request, "laboratory", laboratory_id=laboratory_id, scoped=True)

    @router.get("/laboratories/{resource_id}", operation_id="getLaboratory")
    def laboratory(resource_id: UUID, request: Request):
        return read(request, "laboratory", resource_id)

    @router.post("/laboratories", status_code=201, operation_id="createLaboratory")
    def create_laboratory(body: LaboratoryCreate, request: Request):
        return write(request, "laboratory", body)

    @router.get("/laboratories/{laboratory_id}/locations", operation_id="listLocations")
    def locations(laboratory_id: UUID, request: Request):
        return read(request, "location", laboratory_id=laboratory_id)

    @router.post(
        "/laboratories/{laboratory_id}/locations", status_code=201, operation_id="createLocation"
    )
    def create_location(laboratory_id: UUID, body: LocationCreate, request: Request):
        return write(request, "location", body, laboratory_id=laboratory_id)

    @router.get("/templates", operation_id="listTemplates")
    def templates(request: Request):
        return read(request, "template")

    @router.get("/templates/{resource_id}", operation_id="getTemplate")
    def template(resource_id: UUID, request: Request):
        return read(request, "template", resource_id)

    @router.post("/templates", status_code=201, operation_id="createTemplate")
    def create_template(body: TemplateCreate, request: Request):
        return write(request, "template", body)

    @router.post("/templates/{resource_id}/publish", operation_id="publishTemplate")
    def publish_template(resource_id: UUID, body: VersionCommand, request: Request):
        return write(request, "template", body, operation="publish", resource_id=resource_id)

    @router.post("/templates/{resource_id}/clone", status_code=201, operation_id="cloneTemplate")
    def clone_template(resource_id: UUID, body: VersionCommand, request: Request):
        return write(request, "template", body, operation="clone", resource_id=resource_id)

    @router.get("/inspections", operation_id="listInspections")
    def inspections(request: Request, laboratory_id: UUID | None = None):
        return read(request, "inspection", laboratory_id=laboratory_id, scoped=True)

    @router.get("/inspections/{resource_id}", operation_id="getInspection")
    def inspection(resource_id: UUID, request: Request):
        return read(request, "inspection", resource_id)

    @router.post("/inspections", status_code=201, operation_id="createInspection")
    def create_inspection(body: InspectionCreate, request: Request):
        return write(request, "inspection", body)

    app.include_router(router)
