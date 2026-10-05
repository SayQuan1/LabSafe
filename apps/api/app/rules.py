"""Rule configuration and publication API adapters."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from apps.api.app.foundations import Identifier
from apps.api.app.identity import Body, CanonicalJSONResponse, VersionCommand, pagination
from packages.application.rules import RuleApplication
from packages.domain.security import ServiceError
from packages.persistence.security import SESSION_COOKIE


class RuleSetCreate(Body):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]


class RuleImportBody(Body):
    bundle: dict
    reason: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class RuleSnapshotCreate(Body):
    rule_version_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]


def mount_rules(app, identity):
    app.state.rules = RuleApplication(identity) if identity is not None else None
    router = APIRouter(prefix="/api/v1")

    def context(request, allowed=()):
        if any(
            key not in allowed or len(request.query_params.getlist(key)) != 1
            for key in request.query_params
        ):
            raise ServiceError("VALIDATION_ERROR", 422, "Unexpected or duplicate query parameter")
        if app.state.rules is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Rule service not configured")
        return app.state.rules

    def read(request, kind, resource_id=None):
        allowed = () if resource_id else ("page", "page_size", "laboratory_id")
        service = context(request, allowed)
        page, size = pagination(request)
        laboratory_id = request.query_params.get("laboratory_id")
        if laboratory_id is not None:
            try:
                laboratory_id = str(UUID(laboratory_id))
            except (ValueError, TypeError):
                raise ServiceError("VALIDATION_ERROR", 422, "Invalid laboratory_id") from None
        return service.read(
            kind,
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            resource_id=str(resource_id) if resource_id else None,
            laboratory_id=laboratory_id,
            page=page,
            page_size=size,
        )

    def write(request, kind, operation, body, *, resource_id=None):
        service = context(request)
        status, response = service.write(
            kind,
            operation,
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            body.model_dump(mode="json"),
            request.state.request_id,
            resource_id=str(resource_id) if resource_id else None,
        )
        return CanonicalJSONResponse(response, status_code=status)

    @router.post("/rule-sets", status_code=201, operation_id="createRuleSet")
    def create_rule_set(body: RuleSetCreate, request: Request):
        return write(request, "rule_set", "create", body)

    @router.get("/rule-sets", operation_id="listRuleSets")
    def list_rule_sets(request: Request):
        return read(request, "rule_set")

    @router.get("/rule-versions", operation_id="listRuleVersions")
    def list_rule_versions(request: Request):
        return read(request, "rule_version")

    @router.post("/rule-versions", status_code=201, operation_id="importRuleVersion")
    def import_rule_version(body: RuleImportBody, request: Request):
        return write(request, "rule_version", "import", body)

    @router.get("/rule-versions/{resource_id}", operation_id="getRuleVersion")
    def get_rule_version(resource_id: UUID, request: Request):
        return read(request, "rule_version", resource_id)

    @router.post(
        "/rule-versions/{resource_id}/submit-approval",
        operation_id="submitapprovalRuleVersion",
    )
    def submit_rule_version(resource_id: UUID, body: VersionCommand, request: Request):
        return write(request, "rule_version", "submit", body, resource_id=resource_id)

    @router.post("/rule-versions/{resource_id}/approve", operation_id="approveRuleVersion")
    def approve_rule_version(resource_id: UUID, body: VersionCommand, request: Request):
        return write(request, "rule_version", "approve", body, resource_id=resource_id)

    @router.post("/rule-versions/{resource_id}/publish", operation_id="publishRuleVersion")
    def publish_rule_version(resource_id: UUID, body: VersionCommand, request: Request):
        return write(request, "rule_version", "publish", body, resource_id=resource_id)

    @router.post("/rule-versions/{resource_id}/retire", operation_id="retireRuleVersion")
    def retire_rule_version(resource_id: UUID, body: VersionCommand, request: Request):
        return write(request, "rule_version", "retire", body, resource_id=resource_id)

    @router.get("/rule-bundles", operation_id="listRuleBundles")
    def list_rule_bundles(request: Request):
        return read(request, "rule_bundle")

    @router.post("/rule-bundles", status_code=201, operation_id="createRuleBundle")
    def create_rule_bundle(body: RuleSnapshotCreate, request: Request):
        return write(request, "rule_bundle", "create", body)

    app.include_router(router)
