"""Public identity adapter; no SQL, no client-supplied tenant authority."""

import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis import Redis
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from packages.application.identity import IdentityApplication
from packages.application.rate_limit import RateLimits
from packages.domain.security import ServiceError
from packages.persistence.database import database_engine
from packages.persistence.security import SESSION_COOKIE, SESSION_COOKIE_OPTIONS, SessionService
from packages.shared.json_hash import canonical_json


class CanonicalJSONResponse(JSONResponse):
    def render(self, content):
        return canonical_json(content).encode("utf-8")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Body):
    tenant_code: Annotated[str, Field(min_length=1, max_length=64)]
    username: Annotated[str, Field(min_length=1, max_length=128)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class UserCreate(Body):
    username: Annotated[str, Field(min_length=1, max_length=128)]
    display_name: Annotated[str, Field(min_length=1, max_length=100)]
    initial_password: Annotated[str, Field(min_length=12, max_length=256)]


class VersionCommand(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    reason: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class RoleCommand(Body):
    expected_version: Annotated[int, Field(ge=1, le=2147483647)]
    role: Literal["safety_admin", "lab_manager", "inspector", "remediator", "viewer", "rule_expert"]
    scope_kind: Literal["tenant", "laboratory"]
    laboratory_id: str | None

    @field_validator("laboratory_id")
    @classmethod
    def laboratory_uuid(cls, value):
        if value is None:
            return None
        if len(value) != 36:
            raise ValueError("Invalid laboratory UUID")
        parsed = str(UUID(value))
        if parsed != value.lower():
            raise ValueError("Invalid laboratory UUID")
        return parsed


def pagination(request):
    try:
        page, size = (
            int(request.query_params.get(k, default))
            for k, default in (("page", "1"), ("page_size", "20"))
        )
        if not 1 <= page <= 2147483647 or not 1 <= size <= 100:
            raise ValueError
    except ValueError:
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid pagination") from None
    return page, size


def configured_identity(environment):
    origin = os.environ.get("PUBLIC_ORIGIN", "")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("PUBLIC_ORIGIN must be an HTTPS origin without path or credentials")
    secret = os.environ.get("CSRF_KEY_FILE")
    if not secret:
        raise ValueError("CSRF_KEY_FILE is required")
    key = Path(secret).read_bytes()
    sessions = SessionService(key)
    redis = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=1, socket_timeout=1)
    return IdentityApplication(database_engine(), sessions, RateLimits(redis), environment), origin


def error_response(request, error):
    aliases = {
        "AUTHENTICATION_REQUIRED": "UNAUTHENTICATED",
        "INVALID_CREDENTIALS": "UNAUTHENTICATED",
        "CSRF_FAILED": "FORBIDDEN",
        "LAST_ADMIN": "STATE_CONFLICT",
        "IDEMPOTENCY_EXPIRED": "STATE_CONFLICT",
    }
    code = aliases.get(error.code, error.code)
    headers = {"Cache-Control": "no-store"}
    if error.retry_after is not None:
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse(
        status_code=error.status,
        headers=headers,
        content={
            "request_id": request.state.request_id,
            "error": {
                "code": code,
                "message": str(error),
                "retryable": code
                in {"DEPENDENCY_UNAVAILABLE", "RATE_LIMITED", "REQUEST_IN_PROGRESS"},
                "details": [],
            },
        },
    )


def mount_identity(app, service, origin):
    app.state.identity = service

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        request.state.request_id = str(uuid4())
        try:
            if request.url.path.startswith("/api/v1"):
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    if not origin or request.headers.getlist("origin") != [origin]:
                        raise ServiceError("FORBIDDEN", 403, "Same-origin request required")
                    media = request.headers.get("content-type", "").split(";")[0].lower().strip()
                    if media != "application/json":
                        raise ServiceError("UNSUPPORTED_MEDIA_TYPE", 415, "JSON request required")
                    if request.url.path != "/api/v1/auth/login":
                        for header in ("idempotency-key", "x-csrf-token"):
                            if len(request.headers.getlist(header)) != 1:
                                raise ServiceError(
                                    "VALIDATION_ERROR",
                                    422,
                                    "Required write header missing or duplicated",
                                )
                # A maximal 100-item Unicode template exceeds the identity body's 64 KiB.
                limit = 2097152 if request.url.path == "/api/v1/templates" else 65536
                data = bytearray()
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ServiceError("VALIDATION_ERROR", 422, "Request is too large")
                request._body = bytes(data)
                request.state.identity_body = bytes(data)
            response = await call_next(request)
        except ServiceError as error:
            response = error_response(request, error)
        except Exception:
            # Never return request bodies, exception details, URLs, SQL, or credentials.
            response = error_response(
                request, ServiceError("INTERNAL_ERROR", 500, "Internal error")
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return error_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return error_response(request, ServiceError("VALIDATION_ERROR", 422, "Invalid request"))

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return error_response(
            request, ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Database unavailable")
        )

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        status = 404 if error.status_code == 404 else 422
        return error_response(
            request,
            ServiceError(
                "NOT_FOUND" if status == 404 else "VALIDATION_ERROR",
                status,
                "Request not supported",
            ),
        )

    def context(request, allowed_query=()):
        if any(
            key not in allowed_query or len(request.query_params.getlist(key)) != 1
            for key in request.query_params
        ):
            raise ServiceError("VALIDATION_ERROR", 422, "Unexpected or duplicate query parameter")
        if app.state.identity is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Identity service not configured")
        return app.state.identity

    def write(request, operation, body, user_id=None):
        service = context(request)
        status, response = service.write(
            operation,
            request.cookies.get(SESSION_COOKIE),
            request.headers["x-csrf-token"],
            request.headers["idempotency-key"],
            body,
            request.state.request_id,
            user_id=user_id,
        )
        return CanonicalJSONResponse(response, status_code=status)

    router = APIRouter(prefix="/api/v1")

    @router.post("/auth/login", operation_id="login")
    def login(body: Login, request: Request):
        response, token = context(request).login(
            body.model_dump(),
            request.client.host if request.client else "unknown",
            request.state.request_id,
        )
        result = JSONResponse(response)
        result.set_cookie(SESSION_COOKIE, token, max_age=8 * 3600, **SESSION_COOKIE_OPTIONS)
        return result

    @router.get("/me", operation_id="getMe")
    def me(request: Request):
        return context(request).read(
            "me", request.cookies.get(SESSION_COOKIE), request.state.request_id
        )

    @router.post("/auth/logout", operation_id="logout")
    def logout(request: Request):
        if request.state.identity_body.strip() not in (b"", b"{}"):
            raise ServiceError("VALIDATION_ERROR", 422, "Logout takes no request fields")
        result = write(request, "logout", {})
        result.delete_cookie(SESSION_COOKIE, **SESSION_COOKIE_OPTIONS)
        return result

    @router.get("/users", operation_id="listUsers")
    def users(request: Request):
        service = context(request, ("page", "page_size"))
        page, size = pagination(request)
        return service.read(
            "list",
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            page=page,
            page_size=size,
        )

    @router.get("/users/{user_id}", operation_id="getUser")
    def user(user_id: UUID, request: Request):
        return context(request).read(
            "get",
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            user_id=str(user_id),
        )

    @router.post("/users", status_code=201, operation_id="createUser")
    def create_user(body: UserCreate, request: Request):
        return write(request, "create", body.model_dump())

    @router.post("/users/{user_id}/disable", operation_id="disableUser")
    def disable_user(user_id: UUID, body: VersionCommand, request: Request):
        return write(request, "disable", body.model_dump(), str(user_id))

    @router.get("/users/{user_id}/roles", operation_id="listUserRoles")
    def user_roles(user_id: UUID, request: Request, laboratory_id: UUID | None = None):
        service = context(request, ("page", "page_size", "laboratory_id"))
        page, size = pagination(request)
        return service.read(
            "roles",
            request.cookies.get(SESSION_COOKIE),
            request.state.request_id,
            user_id=str(user_id),
            page=page,
            page_size=size,
            laboratory_id=str(laboratory_id) if laboratory_id is not None else None,
        )

    @router.post("/users/{user_id}/roles", operation_id="grantRole")
    def grant_role(user_id: UUID, body: RoleCommand, request: Request):
        return write(request, "grant_role", body.model_dump(), str(user_id))

    @router.post("/users/{user_id}/revoke-role", operation_id="revokeRole")
    def revoke_role(user_id: UUID, body: RoleCommand, request: Request):
        return write(request, "revoke_role", body.model_dump(), str(user_id))

    app.include_router(router)
