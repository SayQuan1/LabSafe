"""Optional q.general consumer for inference_pipeline tasks."""

from packages.persistence.database import database_engine
from packages.persistence.dispatch import validate_dispatch


def consume_inference(message):
    from packages.application.inference_execution import InferenceExecution

    validate_dispatch(message)
    engine = database_engine()
    try:
        return InferenceExecution(engine).execute(message)
    finally:
        engine.dispose()
