"""Identity use cases: short authorization preflight, then one atomic command.

Redis calls occur outside database transactions. Preflight grants are never used
to skip authentication inside the committing transaction.
"""

from contextlib import contextmanager
from datetime import timedelta
from functools import wraps
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from packages.domain.security import (
    Permission,
    ServiceError,
    authorize,
    permission_projection,
)
from packages.persistence.roles import RoleRepository
from packages.persistence.security import (
    SessionService,
    append_audit,
    begin_idempotency,
    complete_idempotency,
    utc_now,
)
from packages.persistence.transaction import lock_tenant
from packages.persistence.users import UserRepository, timestamp


def retry_deadlocks(command):
    """Retry only rolled-back MySQL deadlocks, at most three complete attempts."""

    @wraps(command)
    def invoke(*args, **kwargs):
        for attempt in range(3):
            try:
                return command(*args, **kwargs)
            except OperationalError as error:
                if getattr(error.orig, "args", [None])[0] != 1213:
                    raise
                if attempt == 2:
                    raise ServiceError(
                        "REQUEST_IN_PROGRESS", 409, "Request is in progress", retry_after=2
                    ) from None

    return invoke


class IdentityApplication:
    def __init__(self, engine, sessions: SessionService, limits, environment: str):
        if environment not in {"dev", "test"}:
            raise ValueError("Identity API is not released for production")
        self.engine, self.sessions, self.limits = engine, sessions, limits
        self.environment = environment
        self.users = UserRepository()
        self.roles = RoleRepository()

    @contextmanager
    def transaction(self):
        try:
            with self.engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection:
                old_timeout = connection.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
                connection.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
                connection.commit()
                try:
                    with connection.begin():
                        yield connection
                finally:
                    connection.execute(
                        text("SET SESSION innodb_lock_wait_timeout=:timeout"),
                        {"timeout": old_timeout},
                    )
                    connection.commit()
        except OperationalError as error:
            errno = getattr(error.orig, "args", [None])[0]
            if errno in {1205, 3572}:
                raise ServiceError(
                    "REQUEST_IN_PROGRESS", 409, "Request is in progress", retry_after=2
                ) from None
            raise

    def readiness(self):
        # This is identity readiness, not the future object/AI/worker readiness gate.
        with self.engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != "0002_report_object_version":
                raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Database revision is not ready")
        self.limits.ready()

    def session_projection(self, connection, session):
        principal = session.principal
        return {
            "user": self.users.get(connection, principal.tenant_id, principal.user_id),
            "tenant_id": principal.tenant_id,
            "csrf_token": session.csrf_token,
            "expires_at": timestamp(session.expires_at),
            "permissions": permission_projection(principal),
            "environment": self.environment,
        }

    def login(self, body, ip, request_id):
        ticket = self.limits.login(body["tenant_code"], body["username"], ip)
        response, token = self._login_once(body, request_id)
        # Failure leaves an unreturned, expiring session, not an unprotected login.
        self.limits.login_succeeded(ticket)
        return response, token

    @retry_deadlocks
    def _login_once(self, body, request_id):
        with self.transaction() as connection:
            session = self.sessions.login(connection, **body)
            append_audit(
                connection,
                principal=session.principal,
                action="session.login",
                resource_type="session",
                resource_id=session.session_id,
                changes={},
                reason="Login",
                request_id=request_id,
            )
            response = {
                "data": self.session_projection(connection, session),
                "request_id": request_id,
            }
        return response, session.token

    @retry_deadlocks
    def _preflight(self, token, *, csrf=None, admin=False):
        with self.transaction() as connection:
            session = self.sessions.authenticate(connection, token, touch=False)
            if csrf is not None:
                self.sessions.require_csrf(session, csrf)
            if admin:
                authorize(session.principal, Permission.ADMIN)
            return session.principal

    def read(
        self,
        operation,
        token,
        request_id,
        *,
        user_id=None,
        page=1,
        page_size=20,
        laboratory_id=None,
    ):
        principal = self._preflight(token, admin=operation != "me")
        self.limits.user(principal, write=False)
        return self._read_once(
            operation,
            token,
            request_id,
            user_id=user_id,
            page=page,
            page_size=page_size,
            laboratory_id=laboratory_id,
        )

    @retry_deadlocks
    def _read_once(
        self,
        operation,
        token,
        request_id,
        *,
        user_id=None,
        page=1,
        page_size=20,
        laboratory_id=None,
    ):
        with self.transaction() as connection:
            session = self.sessions.authenticate(connection, token)
            if operation == "me":
                data = self.session_projection(connection, session)
            else:
                authorize(session.principal, Permission.ADMIN)
                tenant = session.principal.tenant_id
                if operation == "list":
                    data = self.users.list(connection, tenant, page, page_size)
                elif operation == "roles":
                    data = self.roles.list(
                        connection, tenant, user_id, page, page_size, laboratory_id
                    )
                else:
                    data = self.users.get(connection, tenant, user_id)
            return {"data": data, "request_id": request_id}

    def write(self, operation, token, csrf, key, body, request_id, *, user_id=None):
        principal = self._preflight(token, csrf=csrf, admin=operation != "logout")
        self.limits.user(principal, write=True)
        return self._write_once(operation, token, csrf, key, body, request_id, user_id=user_id)

    @retry_deadlocks
    def _write_once(self, operation, token, csrf, key, body, request_id, *, user_id=None):
        paths = {
            "create": "/api/v1/users",
            "disable": f"/api/v1/users/{user_id}/disable",
            "logout": "/api/v1/auth/logout",
            "grant_role": f"/api/v1/users/{user_id}/roles",
            "revoke_role": f"/api/v1/users/{user_id}/revoke-role",
        }
        path = paths[operation]
        with self.transaction() as connection:
            # Scope lookup is not authentication. Administrative tenant exclusion
            # must precede the INSERT's implicit tenant/user FK shared locks to avoid
            # S->X upgrade deadlocks. Idempotency still precedes target aggregate locks.
            tenant, actor_id = self.sessions.locate(connection, token)
            exclusive = operation in {"disable", "grant_role", "revoke_role"}
            if exclusive:
                lock_tenant(connection, tenant, exclusive=True)
            owner = str(uuid4())
            # A keyed digest prevents offline guessing of initial_password from this table.
            request_hash = self.sessions.request_digest(body)
            claim = begin_idempotency(
                connection,
                tenant_id=tenant,
                actor_id=actor_id,
                method="POST",
                path=path,
                key=key,
                request_hash=request_hash,
                expires_at=utc_now() + timedelta(hours=24),
                lease_owner=owner,
            )
            session = self.sessions.authenticate(connection, token, exclusive=exclusive)
            self.sessions.require_csrf(session, csrf)
            actor = session.principal
            if operation != "logout":
                authorize(actor, Permission.ADMIN)
            if user_id is not None:
                self.users.get(connection, tenant, user_id, lock=True)
            if claim.state == "completed":
                return claim.response_status, claim.response
            if operation == "create":
                data = self.users.create(connection, actor, body, request_id)
                status = 201
            elif operation == "disable":
                data = self.users.disable(connection, actor, user_id, body, request_id)
                status = 200
            elif operation in {"grant_role", "revoke_role"}:
                data = self.roles.change(
                    connection, actor, user_id, body, request_id, grant=operation == "grant_role"
                )
                status = 200
            else:
                self.sessions.logout(connection, session)
                append_audit(
                    connection,
                    principal=actor,
                    action="session.logout",
                    resource_type="session",
                    resource_id=session.session_id,
                    changes={},
                    reason="Logout",
                    request_id=request_id,
                )
                data, status = {"ok": True}, 200
            response = {"data": data, "request_id": request_id}
            complete_idempotency(
                connection, claim, status=status, response=response, lease_owner=owner
            )
            return status, response
