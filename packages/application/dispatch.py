"""Publish outside transactions; uncertain delivery deliberately remains retryable."""

import logging
from contextlib import contextmanager
from uuid import uuid4

from packages.persistence import dispatch

LOG = logging.getLogger(__name__)


@contextmanager
def transaction(engine):
    # RC avoids scan-index gap locks; leave API RR connections unchanged.
    with engine.connect() as connection:
        connection = connection.execution_options(isolation_level="READ COMMITTED")
        with connection.begin():
            yield connection


class DispatchPublisher:
    def __init__(self, engine, transport):
        self.engine = engine
        self.transport = transport

    def publish_batch(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Batch limit must be 1..100")
        published = 0
        for _ in range(limit):
            owner = str(uuid4())
            with transaction(self.engine) as connection:
                row = dispatch.claim(connection, owner)
            if row is None:
                break

            def renew():
                with transaction(self.engine) as connection:
                    if not dispatch.owned_update(connection, row, owner, "renew"):
                        raise RuntimeError("Publication lease lost")

            try:
                message = dispatch.validate_dispatch(dispatch.decoded(row["payload"]))
                if (
                    message["tenant_id"] != row["tenant_id"]
                    or message["task_id"] != row["aggregate_id"]
                    or row["aggregate_type"] != "task_run"
                ):
                    raise ValueError("Outbox envelope mismatch")
                # Transport must enforce a 20s wall deadline and heartbeat every 10s.
                self.transport.publish(row["id"], message, renew)
                with transaction(self.engine) as connection:
                    accepted = dispatch.owned_update(connection, row, owner, "published")
                published += int(accepted)
                if not accepted:
                    LOG.warning("dispatch_lease_lost event_id=%s", row["id"])
            except Exception:
                # No exception body/payload/URL in logs; they may include secrets.
                LOG.error("dispatch_publish_failed event_id=%s", row["id"])
                with transaction(self.engine) as connection:
                    dispatch.owned_update(connection, row, owner, "release")
        return published


def sweep_dispatch(engine):
    with transaction(engine) as connection:
        recovered = dispatch.recover_publications(connection)
    with transaction(engine) as connection:
        resent = dispatch.redispatch(connection)
    return recovered, resent
