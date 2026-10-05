"""Worker startup; --check validates configuration without connecting to Redis."""

import argparse
import os

from .app.main import create_celery_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    app = create_celery_app()
    if args.check:
        print("Worker configuration OK (broker connectivity not tested)")
        return
    general = any(
        os.getenv(name, "0") == "1"
        for name in ("WORKER_IMAGE_VALIDATION_ENABLED", "WORKER_INFERENCE_ENABLED")
        + ("WORKER_RULE_EVALUATION_ENABLED",)
    )
    app.worker_main(
        [
            "worker",
            "--loglevel=INFO",
            "--pool=solo",
            "--concurrency=1",
            "--queues=q.general" if general else "--queues=celery",
        ]
    )


if __name__ == "__main__":
    main()
