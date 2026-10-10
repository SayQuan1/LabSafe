"""Authenticated development fixture or pinned resident CPU service."""

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

from .capacity import RequestCapacity
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
    runtime = None
    if settings.mode == "cpu":
        from apps.ai_inference.supervisor import CPUSupervisor

        runtime = CPUSupervisor(settings.bundle, settings.analysis)

    def is_ready():
        return runtime.ready if runtime is not None else ready

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal ready
        if runtime is not None:
            runtime.start()
        else:
            ready = True
        try:
            yield
        finally:
            ready = False
            if runtime is not None:
                await runtime.close()

    app = FastAPI(
        title="LabSafe AI development service",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Exact checked-in wire schema, packaged by the existing design generator.
    app.openapi = lambda: copy.deepcopy(DOCUMENT)
    bearer = HTTPBearer(auto_error=False)
    app.state.supervisor = runtime
    app.add_middleware(RequestCapacity)

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
                "model_bundle_id": settings.identity["model_bundle_id"] if is_ready() else None,
                "active_attempt_id": active_attempt,
            },
        )

    @router.get("/health", operation_id="aiHealth")
    async def health() -> dict[str, Any]:
        return health_payload("alive")

    @router.get("/ready", operation_id="aiReady")
    async def readiness() -> dict[str, Any]:
        if not is_ready():
            raise ProtocolError("MODEL_NOT_READY", "Model is not ready")
        return health_payload("ready")

    @router.get("/version", operation_id="aiVersion")
    async def version() -> dict[str, Any]:
        if runtime is not None and not runtime.ready:
            raise ProtocolError("MODEL_NOT_READY", "Model is not ready")
        return checked("Version", dict(runtime.identity if runtime else settings.identity))

    async def infer(payload: InferenceRequest, stage: str, request: Request) -> dict[str, Any]:
        nonlocal active_attempt
        if not is_ready():
            raise ProtocolError("MODEL_NOT_READY", "Model is not ready")
        body = payload.root
        verify_request(body, settings)
        if gate.locked():
            code = "RUN_IN_PROGRESS" if active_attempt == body["attempt_id"] else "AI_BUSY"
            raise ProtocolError(code, "This instance already has an active attempt")
        async with gate:
            active_attempt = body["attempt_id"]
            try:
                if runtime:
                    task = asyncio.create_task(runtime.infer(body, stage))
                    stop_watching = asyncio.Event()

                    async def disconnected():
                        while not stop_watching.is_set() and not await request.is_disconnected():
                            await asyncio.sleep(0.02)

                    watcher = asyncio.create_task(disconnected())
                    try:
                        done, _ = await asyncio.wait(
                            (task, watcher), return_when=asyncio.FIRST_COMPLETED
                        )
                        if watcher in done and not task.done():
                            task.cancel()
                        value = await task
                    finally:
                        stop_watching.set()
                        watcher.cancel()
                        try:
                            await watcher
                        except asyncio.CancelledError:
                            pass
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                else:
                    value = await compute(body, settings, stage)
                return checked("InferenceResult", value)
            finally:
                active_attempt = None

    @router.post("/quality", operation_id="aiQuality")
    async def quality(payload: InferenceRequest, request: Request) -> dict[str, Any]:
        return await infer(payload, "quality", request)

    @router.post("/runs", operation_id="aiRuns")
    async def runs(payload: InferenceRequest, request: Request) -> dict[str, Any]:
        return await infer(payload, "runs", request)

    app.include_router(router)
    return app
