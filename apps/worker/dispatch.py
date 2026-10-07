"""Opt-in development publisher/sweeper. No business task consumer is enabled."""

import argparse
import logging
import multiprocessing
import os
import time
from threading import Timer

from apps.worker.app.main import create_celery_app
from packages.application.dispatch import DispatchPublisher, sweep_dispatch
from packages.application.inference_execution import InferenceExecution
from packages.application.job_execution import ImageExecution
from packages.application.report_execution import ReportExecution
from packages.application.rule_execution import RuleExecution
from packages.persistence.database import database_engine
from packages.shared.environment import development_environment

PUBLISH_SECONDS = 20
HEARTBEAT_SECONDS = 10


def send_dispatch(event_id, message):
    """Child has no DB connection; hard timeout can terminate a blocked broker call."""
    try:
        app = create_celery_app()
        app.send_task(
            "labsafe.tasks.dispatch",
            args=[message],
            task_id=event_id,
            queue="q.reports" if message.get("task_type") == "report_export" else "q.general",
            serializer="json",
            retry=False,
            headers={"outbox_event_id": event_id},
        )
        app.close()
    except Exception:
        # Suppress tracebacks with broker credentials in a child process.
        raise SystemExit(1) from None


class CeleryDispatchTransport:
    def publish(self, event_id, message, renew):
        child = multiprocessing.get_context("spawn").Process(
            target=send_dispatch,
            args=(event_id, message),
            daemon=True,
        )
        started = time.monotonic()
        child.start()

        def terminate():
            # A slow DB heartbeat must not extend the broker child's wall budget.
            try:
                if child.is_alive():
                    child.kill()
            except ProcessLookupError:
                pass

        deadline = Timer(max(0, PUBLISH_SECONDS - (time.monotonic() - started)), terminate)
        deadline.daemon = True
        deadline.start()
        try:
            child.join(max(0, HEARTBEAT_SECONDS - (time.monotonic() - started)))
            if child.is_alive():
                renew()
                child.join(max(0, PUBLISH_SECONDS - (time.monotonic() - started)))
            if (
                child.is_alive()
                or child.exitcode != 0
                or time.monotonic() - started >= PUBLISH_SECONDS
            ):
                raise RuntimeError("Broker publication unconfirmed")
        finally:
            deadline.cancel()
            deadline.join()
            if child.is_alive():
                child.kill()
            child.join()
            child.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("publisher", "sweeper"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    development_environment()
    if os.getenv("WORKER_DISPATCH_ENABLED", "0") != "1":
        raise SystemExit("Set WORKER_DISPATCH_ENABLED=1 for development dispatch")
    logging.basicConfig(level=logging.INFO)
    engine = database_engine()
    report_enabled = os.getenv("WORKER_REPORT_EXPORT_ENABLED", "0") == "1"
    publisher = DispatchPublisher(engine, CeleryDispatchTransport(), report_enabled=report_enabled)
    try:
        while True:
            try:
                if args.role == "publisher":
                    count = publisher.publish_batch()
                    logging.info("dispatch_published=%d", count)
                else:
                    expired = ImageExecution(engine).recover_expired()
                    expired += InferenceExecution(engine).recover_expired()
                    expired += RuleExecution(engine).recover_expired()
                    expired += ReportExecution(engine).recover_expired()
                    recovered, resent = sweep_dispatch(engine, report_enabled=report_enabled)
                    logging.info(
                        "execution_recovered=%d dispatch_recovered=%d redispatched=%d",
                        expired,
                        recovered,
                        resent,
                    )
            except Exception:
                logging.error("dispatch_cycle_failed")
                if args.once:
                    raise SystemExit(1) from None
            if args.once:
                break
            time.sleep(1 if args.role == "publisher" else 15)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
