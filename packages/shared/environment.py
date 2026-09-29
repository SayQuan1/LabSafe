"""Business-process startup guard; this milestone never enables production."""

import os
import sys


def development_environment() -> str:
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("LabSafe I-01A requires Python 3.11")
    environment = os.environ.get("APP_ENV")
    if environment not in {"dev", "test"}:
        raise RuntimeError("I-01A requires explicit APP_ENV=dev or test; production is disabled")
    return environment
