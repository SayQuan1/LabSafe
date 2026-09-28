"""Worker startup; --check validates configuration without connecting to Redis."""

import argparse

from .app.main import create_celery_app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    app = create_celery_app()
    if args.check:
        print("Worker configuration OK (broker connectivity not tested)")
        return
    app.worker_main(["worker", "--loglevel=INFO"])


if __name__ == "__main__":
    main()
