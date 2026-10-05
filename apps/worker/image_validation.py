"""Bounded, cancellable preparation child; it receives no database or broker handle."""

import multiprocessing
import os
import time

from packages.domain.image_validation import RejectedImage, ValidatedImage
from packages.domain.job_execution import LeaseLost
from packages.domain.security import ServiceError

PREPARE_SECONDS = 120


def prepare_child(channel, settings, tenant, input):
    # Import in the child: real Pillow/storage work is isolated from lease heartbeat.
    from packages.application.image_validation import prepare_image
    from packages.storage.s3 import S3Storage

    storage = None
    try:
        storage = S3Storage(settings)
        channel.send(("result", prepare_image(storage, tenant, input)))
    except ServiceError as error:
        channel.send(("error", error.code))
    except Exception:
        channel.send(("error", "INTERNAL_ERROR"))
    finally:
        channel.close()
        if storage is not None:
            storage.close()


class BoundedImagePrepare:
    def __init__(self, settings, tenant):
        self.settings, self.tenant = settings, tenant

    def __call__(self, input, cancelled):
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        child = context.Process(
            target=prepare_child, args=(send, self.settings, self.tenant, input), daemon=True
        )
        started = time.monotonic()
        try:
            child.start()
            send.close()
            while True:
                if cancelled.is_set():
                    raise LeaseLost()
                remaining = PREPARE_SECONDS - (time.monotonic() - started)
                if remaining <= 0:
                    raise ServiceError("STAGE_TIMEOUT", 503, "Image preparation timed out")
                if receive.poll(min(0.1, remaining)):
                    try:
                        kind, value = receive.recv()
                    except EOFError:
                        raise ServiceError(
                            "INTERNAL_ERROR", 500, "Preparation child exited"
                        ) from None
                    if cancelled.is_set():
                        raise LeaseLost()
                    if time.monotonic() - started >= PREPARE_SECONDS:
                        raise ServiceError("STAGE_TIMEOUT", 503, "Image preparation timed out")
                    if kind == "result" and isinstance(value, (ValidatedImage, RejectedImage)):
                        return value
                    code = (
                        value
                        if kind == "error"
                        and value in {"DEPENDENCY_UNAVAILABLE", "STAGE_TIMEOUT", "INTERNAL_ERROR"}
                        else "INTERNAL_ERROR"
                    )
                    raise ServiceError(code, 503, "Image preparation failed")
                if not child.is_alive():
                    raise ServiceError("INTERNAL_ERROR", 500, "Preparation child exited")
        finally:
            send.close()
            receive.close()
            if child.pid is not None:
                if child.is_alive():
                    child.kill()
                child.join(timeout=2)
                if child.is_alive():
                    raise ServiceError("STAGE_TIMEOUT", 503, "Preparation child did not stop")
                child.close()


def consume_image(message):
    """DB failures leave the lease for sweeper; never pass provider errors to Celery logs."""
    from packages.application.job_execution import ImageExecution
    from packages.persistence.database import database_engine
    from packages.persistence.dispatch import validate_dispatch
    from packages.persistence.image_validation import write_image_result
    from packages.storage.s3 import S3Settings

    validate_dispatch(message)
    settings = S3Settings.from_environment(os.environ["PUBLIC_ORIGIN"], worker=True)
    engine = database_engine()
    try:
        return ImageExecution(engine).execute(
            message,
            BoundedImagePrepare(settings, message["tenant_id"]),
            lambda connection, input, outcome: write_image_result(
                connection, message["tenant_id"], message["task_id"], input, outcome
            ),
        )
    finally:
        engine.dispose()
