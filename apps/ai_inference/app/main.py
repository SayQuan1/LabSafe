"""Authenticated, same-protocol development fixture; no production inference."""

import asyncio
import copy
import hmac
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from packages.inference_protocol.contract import DOCUMENT, InferenceRequest, validate

from .fixture import ProtocolError, compute, verify_request
from .settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if settings.environment not in {"dev", "test"}:
        raise RuntimeError("Production is disabled in I-01A")
    validate("Version", settings.identity)
    gate = asyncio.Lock()
    ready = False
    active_attempt: str | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ready
        ready = True
        try:
            yield
        finally:
            ready = False

    app = FastAPI(
        title="LabSafe AI development fixture",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Exact checked-in wire schema, packaged by the existing design generator.
    app.openapi = lambda: copy.deepcopy(DOCUMENT)
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if credentials is None or not hmac.compare_digest(
            credentials.credentials.encode("utf-8"), settings.token.encode("ascii")
        ):
            raise ProtocolError("UNAUTHENTICATED", "Valid service bearer token required")

    def checked(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        validate(name, payload)
        return payload

    @app.exception_handler(ProtocolError)
    async def protocol_error(request: Request, exc: ProtocolError) -> JSONResponse:
        payload = checked(
            "Error",
            {
                "request_id": str(uuid4()),
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "details": [],
                },
            },
        )
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(payload, status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return await protocol_error(
            request, ProtocolError("VALIDATION_ERROR", "Request violates the inference contract")
        )

    router = APIRouter(prefix=DOCUMENT["servers"][0]["url"], dependencies=[Depends(authenticate)])

    def health_payload(status: str) -> dict[str, Any]:
        return checked(
            "Health",
            {
                "status": status,
                "model_bundle_id": settings.identity["model_bundle_id"],
                "active_attempt_id": active_attempt,
            },
        )

    @router.get("/health", operation_id="aiHealth")
    async def health() -> dict[str, Any]:
        return health_payload("alive")

    @router.get("/ready", operation_id="aiReady")
    async def readiness() -> dict[str, Any]:
        if not ready:
            raise ProtocolError("MODEL_NOT_READY", "Fixture lifespan has not started")
        return health_payload("ready")

    @router.get("/version", operation_id="aiVersion")
    async def version() -> dict[str, Any]:
        return checked("Version", dict(settings.identity))

    async def infer(payload: InferenceRequest, stage: str) -> dict[str, Any]:
        nonlocal active_attempt
        if not ready:
            raise ProtocolError("MODEL_NOT_READY", "Fixture is not ready")
        body = payload.root
        verify_request(body, settings)
        if gate.locked():
            code = "RUN_IN_PROGRESS" if active_attempt == body["attempt_id"] else "AI_BUSY"
            raise ProtocolError(code, "This instance already has an active attempt")
        async with gate:
            active_attempt = body["attempt_id"]
            try:
                return checked("InferenceResult", await compute(body, settings, stage))
            finally:
                active_attempt = None

    @router.post("/quality", operation_id="aiQuality")
    async def quality(payload: InferenceRequest) -> dict[str, Any]:
        return await infer(payload, "quality")

    @router.post("/runs", operation_id="aiRuns")
    async def runs(payload: InferenceRequest) -> dict[str, Any]:
        return await infer(payload, "runs")

    app.include_router(router)
    return app
