"""Real Celery entrypoint; durable routing and execution belong to I-03."""

import logging
import os
from typing import Any

from celery import Celery

from packages.shared.environment import development_environment


def fixture_echo(payload: dict[str, Any]) -> dict[str, Any]:
    """Local smoke task only, NOT an inference or persistence bypass."""
    return {"fixture": True, "payload": payload}


def create_celery_app() -> Celery:
    development_environment()
    celery = Celery("labsafe-worker", broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    celery.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={
            "visibility_timeout": 900,
            "socket_connect_timeout": 2,
            "socket_timeout": 5,
        },
    )
    celery.task(name="labsafe.fixture.echo")(fixture_echo)
    if (
        os.getenv("WORKER_IMAGE_VALIDATION_ENABLED", "0") == "1"
        or os.getenv("WORKER_INFERENCE_ENABLED", "0") == "1"
        or os.getenv("WORKER_RULE_EVALUATION_ENABLED", "0") == "1"
    ):
        from apps.worker.inference_pipeline import consume_inference
        from apps.worker.rule_evaluation import consume_rule

        image_enabled = os.getenv("WORKER_IMAGE_VALIDATION_ENABLED", "0") == "1"
        if image_enabled:
            from apps.worker.image_validation import consume_image
            from packages.storage.s3 import S3Settings

            S3Settings.from_environment(os.environ["PUBLIC_ORIGIN"], worker=True)

        @celery.task(name="labsafe.tasks.dispatch", ignore_result=True, shared=False)
        def dispatch(message):
            try:
                if message.get("task_type") == "inference_pipeline":
                    consume_inference(message)
                elif message.get("task_type") == "rule_evaluation":
                    consume_rule(message)
                else:
                    if not image_enabled:
                        raise RuntimeError("validate_image consumer is disabled")
                    consume_image(message)
            except Exception:
                # DB is the retry/state authority. Do not leak message/provider/secret text.
                logging.getLogger(__name__).warning("image_dispatch_failed")

    return celery
