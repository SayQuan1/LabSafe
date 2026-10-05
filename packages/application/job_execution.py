"""Private execution service and independent lease heartbeat for future handlers."""

import logging
from threading import Event, Thread
from uuid import uuid4

from packages.application.dispatch import transaction
from packages.domain.job_execution import TECHNICAL_ERRORS, LeaseLost
from packages.domain.security import ServiceError
from packages.persistence import job_execution
from packages.shared.environment import development_environment

LOG = logging.getLogger(__name__)


class ImageExecution:
    def __init__(self, engine):
        development_environment()
        self.engine = engine

    def claim(self, message):
        with transaction(self.engine) as connection:
            return job_execution.claim(connection, message, str(uuid4()))

    def heartbeat(self, lease):
        with transaction(self.engine) as connection:
            return job_execution.heartbeat(connection, lease)

    def fail(self, lease, code):
        with transaction(self.engine) as connection:
            return job_execution.fail(connection, lease, code)

    def commit(self, lease, write_result):
        with transaction(self.engine) as connection:
            job_execution.commit_result(connection, lease, write_result)

    def execute(self, message, prepare, write_result):
        """Private handler integration; external prepare never receives a DB connection.

        prepare(input, cancelled) returns an outcome (including content rejection)
        or raises a technical ServiceError. write_result(conn, input, outcome)
        owns all domain/audit/outbox writes. F3 supplies the private server handler.
        """
        lease = self.claim(message)
        if lease is None:
            return False
        cancelled = Event()
        try:
            with LeaseHeartbeat(self, lease, cancelled.set):
                outcome = prepare(lease.input, cancelled)
            self.commit(lease, lambda connection, input: write_result(connection, input, outcome))
        except LeaseLost:
            raise
        except Exception as error:
            code = error.code if isinstance(error, ServiceError) else "INTERNAL_ERROR"
            if code not in TECHNICAL_ERRORS:
                code = "INTERNAL_ERROR"
            self.fail(lease, code)
            raise
        return True

    def recover_expired(self, limit=100):
        with transaction(self.engine) as connection:
            candidates = job_execution.expired_candidates(connection, limit)
        recovered = 0
        for candidate in candidates:
            # Each task owns a short domain-first transaction. A second sweeper
            # rechecks token/expiry and becomes a no-op after the first commits.
            with transaction(self.engine) as connection:
                recovered += int(job_execution.recover(connection, candidate))
        return recovered


class LeaseHeartbeat:
    """A connection per renewal; a failure latches until this attempt is discarded.

    Use around external I/O, then leave the context and call commit(). Callback
    cancellation must be nonblocking. No model calculation belongs on this thread.
    """

    interval = 10

    def __init__(self, execution, lease, cancel):
        self.execution, self.lease, self.cancel = execution, lease, cancel
        self.stop = Event()
        self.lost = Event()
        self.thread = Thread(target=self._loop, name="task-lease-heartbeat", daemon=True)

    def _loop(self):
        while not self.stop.wait(self.interval):
            try:
                if self.execution.heartbeat(self.lease):
                    continue
            except Exception:
                pass
            self.lost.set()
            LOG.warning("task_lease_lost task_id=%s", self.lease.task_id)
            try:
                self.cancel()
            except Exception:
                LOG.warning("task_cancel_failed task_id=%s", self.lease.task_id)
            return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            # A stuck database operation must never allow the caller to commit.
            self.lost.set()
        if self.lost.is_set():
            # Cancellation often makes prepare raise. That exception must not
            # turn lost ownership into a new technical-failure write.
            raise LeaseLost() from None
        return False
