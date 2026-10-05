"""Rule evaluation worker orchestration."""

from threading import Event, Thread
from uuid import uuid4

from packages.application.dispatch import transaction
from packages.domain.rule_execution import RuleLeaseLost
from packages.persistence import rule_execution
from packages.rules.evaluator import RuleDefinitionError, evaluate_bundle
from packages.shared.environment import development_environment


class RuleHeartbeat:
    interval = 10

    def __init__(self, execution, lease, cancel):
        self.execution, self.lease, self.cancel = execution, lease, cancel
        self.stop = Event()
        self.lost = Event()
        self.thread = Thread(target=self._loop, name="rule-lease-heartbeat", daemon=True)

    def _loop(self):
        while not self.stop.wait(self.interval):
            try:
                renewed = self.execution.heartbeat(self.lease)
            except Exception:
                renewed = False
            if renewed:
                continue
            self.lost.set()
            try:
                self.cancel()
            except Exception:
                pass
            return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive() or self.lost.is_set():
            raise RuleLeaseLost() from None
        return False


class RuleExecution:
    def __init__(self, engine):
        development_environment()
        self.engine = engine

    def claim(self, message):
        with transaction(self.engine) as connection:
            return rule_execution.claim(connection, message, str(uuid4()))

    def heartbeat(self, lease):
        with transaction(self.engine) as connection:
            return rule_execution.heartbeat(connection, lease)

    def fail(self, lease, code):
        with transaction(self.engine) as connection:
            return rule_execution.fail(connection, lease, code)

    def commit(self, lease):
        # Rule evaluation is pure over the immutable lease snapshot.  Keep it
        # outside the database transaction; the persistence layer re-locks the
        # current item/evaluation/task and fences the write before applying it.
        try:
            results = evaluate_bundle(
                lease.input.bundle,
                lease.input.facts,
                lease.input.reference_date,
                tenant_id=lease.input.tenant_id,
                laboratory_id=lease.input.laboratory_id,
            )
        except RuleDefinitionError:
            with transaction(self.engine) as connection:
                return rule_execution.commit_result(connection, lease, error_code="RULESET_INVALID")
        with transaction(self.engine) as connection:
            return rule_execution.commit_result(connection, lease, results=results)

    def recover_expired(self, limit=100):
        with transaction(self.engine) as connection:
            candidates = rule_execution.expired_candidates(connection, limit)
        recovered = 0
        for candidate in candidates:
            with transaction(self.engine) as connection:
                recovered += int(rule_execution.recover(connection, candidate))
        return recovered

    def execute(self, message):
        lease = self.claim(message)
        if lease is None:
            return False
        try:
            with RuleHeartbeat(self, lease, lambda: None):
                self.commit(lease)
        except RuleLeaseLost:
            raise
        except Exception:
            self.fail(lease, "INTERNAL_ERROR")
            raise
        return True
