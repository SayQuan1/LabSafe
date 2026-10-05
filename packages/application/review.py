"""Application transactions for fact review and finding decisions."""

from datetime import timedelta
from uuid import UUID, uuid4

from packages.application.identity import retry_deadlocks
from packages.domain.security import ServiceError
from packages.persistence import review as repository
from packages.persistence.security import begin_idempotency, complete_idempotency, utc_now
from packages.persistence.transaction import lock_tenant


class ReviewApplication:
    def __init__(self, identity):
        self.identity = identity

    def read(
        self,
        kind,
        token,
        request_id,
        *,
        resource_id=None,
        laboratory_id=None,
        page=1,
        page_size=20,
        current_only=True,
        status=None,
        severity=None,
    ):
        principal = self.identity._preflight(token)
        self.identity.limits.user(principal, write=False)
        return self._read_once(
            kind,
            token,
            request_id,
            resource_id=resource_id,
            laboratory_id=laboratory_id,
            page=page,
            page_size=page_size,
            current_only=current_only,
            status=status,
            severity=severity,
        )

    @retry_deadlocks
    def _read_once(
        self,
        kind,
        token,
        request_id,
        *,
        resource_id,
        laboratory_id,
        page,
        page_size,
        current_only,
        status,
        severity,
    ):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            if kind == "facts":
                data = repository.current_facts(connection, actor, resource_id)
            elif kind == "finding":
                data = repository.get_finding(connection, actor, resource_id)
            elif kind == "findings":
                data = repository.list_findings(
                    connection,
                    actor,
                    page,
                    page_size,
                    laboratory_id=laboratory_id,
                    current_only=current_only,
                    status=status,
                    severity=severity,
                )
            else:
                raise ServiceError("VALIDATION_ERROR", 422, "Unknown review query")
            return {"data": data, "request_id": request_id}

    def write(self, operation, token, csrf, key, resource_id, body, request_id):
        principal = self.identity._preflight(token, csrf=csrf)
        self.identity.limits.user(principal, write=True)
        try:
            resource_id = str(UUID(resource_id))
        except (TypeError, ValueError):
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid resource id") from None
        if operation not in {"facts", "confirm", "reject", "cannot-determine"}:
            raise ServiceError("VALIDATION_ERROR", 422, "Unknown review command")
        if not isinstance(body, dict):
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid review command body")
        return self._write_once(operation, token, csrf, key, resource_id, body, request_id)

    @retry_deadlocks
    def _write_once(self, operation, token, csrf, key, resource_id, body, request_id):
        path = (
            f"/api/v1/inspection-items/{resource_id}/facts"
            if operation == "facts"
            else f"/api/v1/findings/{resource_id}/{operation}"
        )
        with self.identity.transaction() as connection:
            tenant, actor_id = self.identity.sessions.locate(connection, token)
            lock_tenant(connection, tenant)
            owner = str(uuid4())
            claim = begin_idempotency(
                connection,
                tenant_id=tenant,
                actor_id=actor_id,
                method="POST",
                path=path,
                key=key,
                request_hash=self.identity.sessions.request_digest(body),
                expires_at=utc_now() + timedelta(hours=24),
                lease_owner=owner,
            )
            session = self.identity.sessions.authenticate(connection, token)
            self.identity.sessions.require_csrf(session, csrf)
            actor = session.principal
            if claim.state == "completed":
                if operation == "facts":
                    repository.authorize_item(connection, actor, resource_id)
                else:
                    repository.authorize_finding(connection, actor, resource_id)
                return claim.response_status, claim.response
            if operation == "facts":
                data = repository.edit_facts(connection, actor, resource_id, body, request_id)
                status = 202
            else:
                data = repository.decide_finding(
                    connection, actor, resource_id, body, operation, request_id
                )
                status = 200
            response = {"data": data, "request_id": request_id}
            complete_idempotency(
                connection, claim, status=status, response=response, lease_owner=owner
            )
            return status, response
