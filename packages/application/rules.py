"""Application boundary for tenant rule configuration and publication."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from packages.application.identity import retry_deadlocks
from packages.domain.security import ServiceError
from packages.persistence.rules import RuleRepository
from packages.persistence.security import begin_idempotency, complete_idempotency, utc_now
from packages.persistence.transaction import lock_tenant


class RuleApplication:
    def __init__(self, identity):
        self.identity = identity
        self.rules = RuleRepository()

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
    ):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            if kind == "rule_set":
                data = (
                    self.rules.get_set(connection, actor, resource_id)
                    if resource_id
                    else self.rules.list_sets(connection, actor, page, page_size, laboratory_id)
                )
            elif kind == "rule_version":
                data = (
                    self.rules.get_version(connection, actor, resource_id)
                    if resource_id
                    else self.rules.list_versions(connection, actor, page, page_size, laboratory_id)
                )
            elif kind == "rule_bundle":
                data = self.rules.list_bundles(connection, actor, page, page_size, laboratory_id)
            else:
                raise ValueError("Unknown rule resource")
            return {"data": data, "request_id": request_id}

    def write(self, kind, operation, token, csrf, key, body, request_id, *, resource_id=None):
        principal = self.identity._preflight(token, csrf=csrf)
        self.identity.limits.user(principal, write=True)
        try:
            return self._write_once(
                kind,
                operation,
                token,
                csrf,
                key,
                body,
                request_id,
                resource_id=resource_id,
            )
        except IntegrityError as error:
            if getattr(error.orig, "args", [None])[0] == 1062:
                raise ServiceError("STATE_CONFLICT", 409, "Resource already exists") from None
            raise

    @retry_deadlocks
    def _write_once(self, kind, operation, token, csrf, key, body, request_id, *, resource_id):
        paths = {
            "rule_set": "/api/v1/rule-sets",
            "rule_version": "/api/v1/rule-versions",
            "rule_bundle": "/api/v1/rule-bundles",
        }
        path = paths[kind]
        if resource_id is not None:
            path += f"/{resource_id}/{operation}"
        with self.identity.transaction() as connection:
            tenant, user = self.identity.sessions.locate(connection, token)
            lock_tenant(connection, tenant)
            owner = str(uuid4())
            claim = begin_idempotency(
                connection,
                tenant_id=tenant,
                actor_id=user,
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
                if kind == "rule_set":
                    self.rules.get_set(connection, actor, claim.response["data"]["id"], share=True)
                elif kind == "rule_version":
                    self.rules.get_version(
                        connection, actor, claim.response["data"]["id"], share=True
                    )
                else:
                    # Bundle create is idempotent by checksum and the cached
                    # projection is tenant-scoped by the authenticated actor.
                    pass
                return claim.response_status, claim.response
            if kind == "rule_set":
                data = self.rules.create_set(connection, actor, body, request_id)
                status = 201
            elif kind == "rule_version" and operation == "import":
                data = self.rules.import_version(connection, actor, body, request_id)
                status = 201
            elif kind == "rule_version":
                data = self.rules.transition(
                    connection, actor, resource_id, operation, body, request_id
                )
                status = 200
            elif kind == "rule_bundle":
                data = self.rules.create_bundle(connection, actor, body, request_id)
                status = 201
            else:
                raise ValueError("Unknown rule command")
            response = {"data": data, "request_id": request_id}
            complete_idempotency(
                connection, claim, status=status, response=response, lease_owner=owner
            )
            return status, response
