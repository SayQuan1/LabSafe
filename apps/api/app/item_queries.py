"""Read-only public inspection item endpoints."""

from uuid import UUID

from fastapi import APIRouter, Request

from apps.api.app.identity import pagination
from packages.application.item_queries import ItemQueryApplication
from packages.domain.security import ServiceError
from packages.persistence.security import SESSION_COOKIE


def mount_item_queries(app, identity):
    app.state.item_queries = ItemQueryApplication(identity) if identity is not None else None
    router = APIRouter(prefix="/api/v1")

    def service(request, allowed=()):
        if any(
            key not in allowed or len(request.query_params.getlist(key)) != 1
            for key in request.query_params
        ):
            raise ServiceError("VALIDATION_ERROR", 422, "Unexpected or duplicate query parameter")
        if app.state.item_queries is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Item query service not configured")
        return app.state.item_queries

    @router.get("/inspections/{inspection_id}/items", operation_id="listInspectionItems")
    def items(inspection_id: UUID, request: Request, laboratory_id: UUID | None = None):
        application = service(request, ("page", "page_size", "laboratory_id"))
        page, size = pagination(request)
        return application.read(
            "list",
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            resource_id=str(inspection_id),
            page=page,
            page_size=size,
            laboratory_id=str(laboratory_id) if laboratory_id is not None else None,
        )

    @router.get("/inspection-items/{item_id}", operation_id="getInspectionItem")
    def item(item_id: UUID, request: Request):
        return service(request).read(
            "get",
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            resource_id=str(item_id),
        )

    app.include_router(router)
