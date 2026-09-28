"""Real Celery entrypoint; durable routing and execution belong to I-03."""

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
    )
    celery.task(name="labsafe.fixture.echo")(fixture_echo)
    return celery
