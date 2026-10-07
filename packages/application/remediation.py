"""Application transactions for remediation commands and evidence."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from packages.application.identity import retry_deadlocks
from packages.domain.security import ServiceError
from packages.persistence import remediation as repository
from packages.persistence.security import begin_idempotency, complete_idempotency, utc_now
from packages.persistence.transaction import lock_tenant


class RemediationApplication:
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
        status=None,
        overdue=None,
    ):
        actor = self.identity._preflight(token)
        self.identity.limits.user(actor, write=False)
        return self._read_once(
            kind,
            token,
            request_id,
            resource_id,
            laboratory_id,
            page,
            page_size,
            status,
            overdue,
        )

    @retry_deadlocks
    def _read_once(
        self,
        kind,
        token,
        request_id,
        resource_id,
        laboratory_id,
        page,
        page_size,
        status,
        overdue,
    ):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            if kind == "task":
                data = repository.get_task(connection, actor, resource_id)
            elif kind == "tasks":
                data = repository.list_tasks(
                    connection, actor, page, page_size, laboratory_id, status, overdue
                )
            elif kind == "evidence":
                data = repository.list_evidence(
                    connection, actor, resource_id, page, page_size, laboratory_id
                )
            else:
                raise ServiceError("VALIDATION_ERROR", 422, "Unknown remediation query")
            return {"data": data, "request_id": request_id}

    def write(self, operation, token, csrf, key, resource_id, body, request_id):
        actor = self.identity._preflight(token, csrf=csrf)
        self.identity.limits.user(actor, write=True)
        try:
            resource_id = str(UUID(resource_id))
        except (TypeError, ValueError):
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid resource id") from None
        if operation not in {
            "dispatch",
            "accept",
            "submit-evidence",
            "recheck",
            "reject",
            "cannot-remediate",
            "reassign",
        }:
            raise ServiceError("VALIDATION_ERROR", 422, "Unknown remediation command")
        return self._write_once(operation, token, csrf, key, resource_id, body, request_id)

    @retry_deadlocks
    def _write_once(self, operation, token, csrf, key, resource_id, body, request_id):
        path = (
            f"/api/v1/findings/{resource_id}/remediation-tasks"
            if operation == "dispatch"
            else f"/api/v1/remediation-tasks/{resource_id}/{operation}"
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
            if operation in {"dispatch", "reassign"} and isinstance(body.get("due_at"), str):
                body = {
                    **body,
                    "due_at": datetime.fromisoformat(body["due_at"].replace("Z", "+00:00")),
                }
            if claim.state == "completed":
                if operation == "dispatch":
                    repository.authorize_finding(connection, actor, resource_id)
                else:
                    repository.get_task(connection, actor, resource_id)
                return claim.response_status, claim.response
            if operation == "dispatch":
                data = repository.dispatch(connection, actor, resource_id, body, request_id)
                status = 201
            elif operation == "submit-evidence":
                data = repository.submit(connection, actor, resource_id, body, request_id)
                status = 200
            else:
                data = repository.command(
                    connection, actor, resource_id, body, operation, request_id
                )
                status = 200
            response = {"data": data, "request_id": request_id}
            complete_idempotency(
                connection, claim, status=status, response=response, lease_owner=owner
            )
            return status, response
