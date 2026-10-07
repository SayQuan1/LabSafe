"""Execute frozen CSV reports with independent heartbeat and fenced result writes."""

from threading import Event
from uuid import uuid4

from packages.application.dispatch import transaction
from packages.application.job_execution import LeaseHeartbeat
from packages.domain.job_execution import LeaseLost
from packages.domain.report_execution import REPORT_ERRORS, ReportInvalid
from packages.domain.security import ServiceError
from packages.persistence import report_execution
from packages.shared.environment import development_environment


class ReportExecution:
    def __init__(self, engine):
        development_environment()
        self.engine = engine

    def claim(self, message):
        with transaction(self.engine) as connection:
            return report_execution.claim(connection, message, str(uuid4()))

    def heartbeat(self, lease):
        with transaction(self.engine) as connection:
            return report_execution.heartbeat(connection, lease)

    def fail(self, lease, code):
        with transaction(self.engine) as connection:
            return report_execution.fail(connection, lease, code)

    def commit(self, lease, artifact):
        with transaction(self.engine) as connection:
            return report_execution.commit_result(connection, lease, artifact)

    def execute(self, message, prepare):
        lease = self.claim(message)
        if lease is None:
            return False
        cancelled = Event()
        try:
            with LeaseHeartbeat(self, lease, cancelled.set):
                artifact = prepare(lease.input, cancelled)
            self.commit(lease, artifact)
        except LeaseLost:
            raise
        except Exception as error:
            code = error.code if isinstance(error, ServiceError) else "INTERNAL_ERROR"
            if isinstance(error, ReportInvalid):
                code = "SCHEMA_MISMATCH"
            self.fail(lease, code if code in REPORT_ERRORS else "INTERNAL_ERROR")
            raise
        return True

    def recover_expired(self, limit=100):
        with transaction(self.engine) as connection:
            candidates = report_execution.expired_candidates(connection, limit)
        recovered = 0
        for candidate in candidates:
            with transaction(self.engine) as connection:
                recovered += int(report_execution.recover(connection, candidate))
        return recovered
