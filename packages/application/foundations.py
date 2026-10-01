"""Foundation use cases sharing identity's transaction and authorization boundary."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from packages.application.identity import retry_deadlocks
from packages.domain.security import Permission, ServiceError, authorize
from packages.persistence.inspections import InspectionRepository
from packages.persistence.organizations import OrganizationRepository
from packages.persistence.security import begin_idempotency, complete_idempotency, utc_now
from packages.persistence.templates import TemplateRepository


class FoundationApplication:
    def __init__(self, identity):
        self.identity = identity
        self.organizations = OrganizationRepository()
        self.templates = TemplateRepository()
        self.inspections = InspectionRepository()

    def read(
        self, kind, token, request_id, *, resource_id=None, laboratory_id=None, page=1, page_size=20
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
    def _read_once(self, kind, token, request_id, *, resource_id, laboratory_id, page, page_size):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            if kind in {"college", "laboratory", "location"}:
                if resource_id is not None:
                    data = self.organizations.get(connection, actor, kind, resource_id)
                else:
                    data = self.organizations.list(
                        connection, actor, kind, page, page_size, laboratory_id
                    )
            elif kind == "template":
                data = (
                    self.templates.get(connection, actor, resource_id)
                    if resource_id
                    else self.templates.list(connection, actor, page, page_size)
                )
            elif kind == "inspection":
                data = (
                    self.inspections.get(connection, actor, resource_id)
                    if resource_id
                    else self.inspections.list(connection, actor, page, page_size, laboratory_id)
                )
            else:
                raise ValueError("Unknown foundation resource")
            return {"data": data, "request_id": request_id}

    def write(
        self,
        kind,
        operation,
        token,
        csrf,
        key,
        body,
        request_id,
        *,
        resource_id=None,
        laboratory_id=None,
    ):
        principal = self.identity._preflight(token, csrf=csrf, admin=kind != "inspection")
        if kind == "inspection":
            authorize(principal, Permission.CAPTURE, body["laboratory_id"])
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
                laboratory_id=laboratory_id,
            )
        except IntegrityError as error:
            if getattr(error.orig, "args", [None])[0] == 1062:
                raise ServiceError("STATE_CONFLICT", 409, "Resource already exists") from None
            raise

    def check_visibility(self, connection, actor, kind, body, laboratory_id):
        if kind == "inspection":
            authorize(actor, Permission.CAPTURE, body["laboratory_id"])
            self.organizations.get(connection, actor, "laboratory", body["laboratory_id"])
        else:
            authorize(actor, Permission.ADMIN)
        if kind == "location":
            self.organizations.get(connection, actor, "laboratory", laboratory_id)
        if kind == "laboratory":
            self.organizations.get(connection, actor, "college", body["college_id"])

    @retry_deadlocks
    def _write_once(
        self, kind, operation, token, csrf, key, body, request_id, *, resource_id, laboratory_id
    ):
        paths = {
            "college": "/api/v1/colleges",
            "laboratory": "/api/v1/laboratories",
            "location": f"/api/v1/laboratories/{laboratory_id}/locations",
            "template": "/api/v1/templates",
            "inspection": "/api/v1/inspections",
        }
        path = paths[kind]
        if operation != "create":
            if kind != "template" or operation not in {"publish", "clone"}:
                raise ValueError("Unknown foundation command")
            path += f"/{resource_id}/{operation}"
        with self.identity.transaction() as connection:
            tenant, actor_id = self.identity.sessions.locate(connection, token)
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
            self.check_visibility(connection, actor, kind, body, laboratory_id)
            if claim.state == "completed":
                # Revalidate current scope before exposing a cached resource, without rerunning
                # version/state preconditions or creating another resource.
                result_id = claim.response["data"]["id"]
                if kind == "inspection":
                    self.inspections.get(connection, actor, result_id, share=True)
                elif kind == "template":
                    if resource_id is not None:
                        self.templates.get(connection, actor, resource_id, share=True)
                    self.templates.get(connection, actor, result_id, share=True)
                elif kind == "location":
                    self.organizations.row(
                        connection, tenant, kind, result_id, laboratory_id=laboratory_id
                    )
                else:
                    self.organizations.get(connection, actor, kind, result_id)
                return claim.response_status, claim.response
            if kind in {"college", "laboratory", "location"}:
                data = self.organizations.create(
                    connection, actor, kind, body, request_id, laboratory_id=laboratory_id
                )
            elif kind == "inspection":
                data = self.inspections.create(connection, actor, body, request_id)
            elif operation == "create":
                data = self.templates.create(connection, actor, body, request_id)
            else:
                data = self.templates.change(
                    connection, actor, resource_id, body, request_id, clone=operation == "clone"
                )
            status = 200 if operation == "publish" else 201
            response = {"data": data, "request_id": request_id}
            complete_idempotency(
                connection, claim, status=status, response=response, lease_owner=owner
            )
            return status, response
