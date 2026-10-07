"""CSV generation/upload child with a wall deadline and no database handles."""

import multiprocessing
import os
import time

from packages.domain.job_execution import LeaseLost
from packages.domain.report_execution import (
    REPORT_ERRORS,
    REPORT_SECONDS,
    ReportArtifact,
    ReportInvalid,
    csv_bytes,
    validate_artifact,
)
from packages.domain.security import ServiceError


def prepare_child(channel, settings, source):
    from packages.storage.s3 import S3Storage

    storage = None
    try:
        payload = csv_bytes(source)
        storage = S3Storage(settings)
        channel.send(("result", storage.put_report(source, payload)))
    except ReportInvalid:
        channel.send(("error", "SCHEMA_MISMATCH"))
    except ServiceError as error:
        channel.send(("error", error.code if error.code in REPORT_ERRORS else "INTERNAL_ERROR"))
    except Exception:
        channel.send(("error", "INTERNAL_ERROR"))
    finally:
        channel.close()
        if storage is not None:
            storage.close()


class BoundedReportPrepare:
    def __init__(self, settings):
        self.settings = settings

    def __call__(self, source, cancelled):
        if cancelled.is_set():
            raise LeaseLost()
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        child = context.Process(
            target=prepare_child, args=(send, self.settings, source), daemon=True
        )
        started = time.monotonic()
        try:
            child.start()
            send.close()
            while True:
                if cancelled.is_set():
                    raise LeaseLost()
                remaining = REPORT_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise ServiceError("STAGE_TIMEOUT", 504, "Report preparation timed out")
                if receive.poll(min(0.1, remaining)):
                    try:
                        kind, value = receive.recv()
                    except EOFError:
                        raise ServiceError("INTERNAL_ERROR", 500, "Report child exited") from None
                    if cancelled.is_set():
                        raise LeaseLost()
                    if time.monotonic() - started >= REPORT_SECONDS:
                        raise ServiceError("STAGE_TIMEOUT", 504, "Report preparation timed out")
                    if kind == "result" and isinstance(value, ReportArtifact):
                        validate_artifact(source, value)
                        return value
                    code = (
                        value
                        if kind == "error" and isinstance(value, str) and value in REPORT_ERRORS
                        else "INTERNAL_ERROR"
                    )
                    raise ServiceError(
                        code,
                        503 if code == "DEPENDENCY_UNAVAILABLE" else 500,
                        "Report preparation failed",
                    )
                if not child.is_alive():
                    raise ServiceError("INTERNAL_ERROR", 500, "Report child exited")
        finally:
            send.close()
            receive.close()
            if child.pid is not None:
                if child.is_alive():
                    child.kill()
                child.join(timeout=2)
                if child.is_alive():
                    raise ServiceError("STAGE_TIMEOUT", 504, "Report child did not stop")
                child.close()


def consume_report(message):
    from packages.application.report_execution import ReportExecution
    from packages.persistence.database import database_engine
    from packages.persistence.dispatch import validate_dispatch
    from packages.storage.s3 import S3Settings

    validate_dispatch(message)
    settings = S3Settings.from_environment(os.environ["PUBLIC_ORIGIN"], worker=True)
    engine = database_engine()
    try:
        return ReportExecution(engine).execute(message, BoundedReportPrepare(settings))
    finally:
        engine.dispose()
