"""Manual image-job replay: authenticate, observe fixed input outside locks, revalidate, commit."""

from datetime import timedelta
from uuid import uuid4

from packages.application.identity import retry_deadlocks
from packages.domain.job_replay import replay_body, require_replay
from packages.domain.security import Permission, ServiceError, authorize
from packages.persistence.jobs import JobRepository
from packages.persistence.security import begin_idempotency, complete_idempotency, utc_now
from packages.persistence.transaction import lock_tenant
from packages.storage.s3 import ObjectVersion


class _NeedsPinnedInspection(Exception):
    def __init__(self, tenant, input):
        self.tenant, self.input = tenant, input


class JobApplication:
    def __init__(self, identity, storage=None):
        self.identity, self.storage = identity, storage
        self.jobs = JobRepository()

    def read(
        self, operation, token, request_id, *, job_id=None, page=1, page_size=20, laboratory_id=None
    ):
        actor = self.identity._preflight(token, admin=operation == "dead_letters")
        self.identity.limits.user(actor, write=False)
        return self._read_once(operation, token, request_id, job_id, page, page_size, laboratory_id)

    @retry_deadlocks
    def _read_once(self, operation, token, request_id, job_id, page, size, lab):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            if operation == "get":
                data = self.jobs.get(connection, actor, job_id)
            elif operation == "dead_letters":
                data = self.jobs.dead_letters(connection, actor, page, size, lab)
            else:
                raise ValueError("Unknown job query")
            return {"data": data, "request_id": request_id}

    def replay(self, token, csrf, key, job_id, body, request_id):
        replay_body(body)
        actor = self.identity._preflight(token, csrf=csrf, admin=True)
        self.identity.limits.user(actor, write=True)
        try:
            return self._replay_once(token, csrf, key, job_id, body, request_id)
        except _NeedsPinnedInspection as needed:
            tenant, input = needed.tenant, needed.input
        if self.storage is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Pinned storage check not configured")
        reference = ObjectVersion(
            input.key, input.object_version, input.size_bytes, input.mime_type
        )
        try:
            self.storage.inspect_pinned_staging(tenant, input.upload_id, reference)
        except ServiceError as error:
            if error.code == "OBJECT_NOT_FOUND":
                raise ServiceError("STATE_CONFLICT", 409, "Pinned input no longer exists") from None
            raise
        return self._replay_once(token, csrf, key, job_id, body, request_id, observed=input)

    @retry_deadlocks
    def _replay_once(self, token, csrf, key, job_id, body, request_id, *, observed=None):
        with self.identity.transaction() as connection:
            tenant, user = self.identity.sessions.locate(connection, token)
            lock_tenant(connection, tenant)
            lease = str(uuid4())
            claim = begin_idempotency(
                connection,
                tenant_id=tenant,
                actor_id=user,
                method="POST",
                path=f"/api/v1/dead-letters/{job_id}/replay",
                key=key,
                request_hash=self.identity.sessions.request_digest(body),
                expires_at=utc_now() + timedelta(hours=24),
                lease_owner=lease,
            )
            session = self.identity.sessions.authenticate(connection, token)
            self.identity.sessions.require_csrf(session, csrf)
            actor = session.principal
            authorize(actor, Permission.ADMIN)
            if claim.state == "completed":
                # Recheck current visibility/admin, not obsolete state/version or object lifetime.
                self.jobs.get(connection, actor, job_id)
                return claim.response_status, claim.response
            task, current = self.jobs.replay_source(connection, actor, job_id)
            require_replay(actor, task, current, body["expected_version"])
            if observed is None:
                raise _NeedsPinnedInspection(
                    tenant, current
                )  # Rolls back provisional claim and locks.
            if current != observed:
                raise ServiceError("STATE_CONFLICT", 409, "Pinned input changed during inspection")
            data = self.jobs.replay(connection, actor, task, body["reason"], request_id)
            response = {"data": data, "request_id": request_id}
            complete_idempotency(
                connection, claim, status=202, response=response, lease_owner=lease
            )
            return 202, response
