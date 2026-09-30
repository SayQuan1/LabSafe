"""Transaction-bound sessions and append-only audit; no public auth routes yet.

The API adapter must also enforce Origin/JSON, rate limiting and cookie options.
Application commands own commit/rollback. SQL never trusts a client tenant ID.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from packages.domain.security import Permission, Principal, ServiceError, authorize
from packages.persistence.bootstrap import PASSWORD_HASHER, normalized
from packages.persistence.database import DatabaseSafetyError
from packages.persistence.transaction import lock_tenant, require_transaction, utc_now
from packages.shared.json_hash import canonical_json

SESSION_COOKIE = "labsafe_session"
SESSION_COOKIE_OPTIONS = {"secure": True, "httponly": True, "samesite": "lax", "path": "/"}
SESSION_ABSOLUTE = timedelta(hours=8)
SESSION_IDLE = timedelta(minutes=30)
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
AuthError = ServiceError


def authentication_required() -> ServiceError:
    return ServiceError("AUTHENTICATION_REQUIRED", 401, "Authentication required")


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def safe_text(value: str, limit: int) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        return normalized(value, limit)
    except (ValueError, TypeError):
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid text") from None


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: str
    token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    principal: Principal
    expires_at: datetime
    idle_expires_at: datetime


@dataclass(frozen=True)
class IdempotencyClaim:
    record_id: str
    state: str
    response_status: int | None = None
    response: Any = None
    lease_owner: str | None = None


def load_principal(connection: Connection, tenant_id: str, user_id: str) -> Principal:
    # A locking/current read avoids old REPEATABLE READ snapshots after lock waits.
    rows = (
        connection.execute(
            text(
                "SELECT u.id,u.tenant_id,u.username,r.code,ur.scope_kind,ur.laboratory_id "
                "FROM users u LEFT JOIN user_roles ur ON ur.tenant_id=u.tenant_id "
                "AND ur.user_id=u.id "
                "LEFT JOIN roles r ON r.id=ur.role_id "
                "WHERE u.tenant_id=:tenant AND u.id=:user AND u.status='active' FOR SHARE"
            ),
            {"tenant": tenant_id, "user": user_id},
        )
        .mappings()
        .all()
    )
    if not rows:
        raise authentication_required()
    return Principal(
        user_id,
        tenant_id,
        rows[0]["username"],
        tuple(
            (row["code"], row["scope_kind"], row["laboratory_id"])
            for row in rows
            if row["code"] is not None
        ),
    )


class SessionService:
    def __init__(self, csrf_key: bytes):
        if not isinstance(csrf_key, bytes) or len(csrf_key) < 32:
            raise DatabaseSafetyError("CSRF key must contain at least 32 random bytes")
        self._csrf_key = csrf_key
        self._dummy_hash = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))

    def _csrf(self, token: str) -> str:
        digest = hmac.new(self._csrf_key, token.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def login(
        self, connection: Connection, *, tenant_code: str, username: str, password: str
    ) -> AuthenticatedSession:
        require_transaction(connection)
        failure = ServiceError("INVALID_CREDENTIALS", 401, "账号或密码错误")
        try:
            tenant_code = normalized(tenant_code, 64)
            username = normalized(username, 128, account=True)
        except (ValueError, TypeError):
            raise failure from None
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise failure
        tenant = connection.scalar(
            text("SELECT id FROM tenants WHERE code=:code"), {"code": tenant_code}
        )
        row = None
        if tenant:
            try:
                lock_tenant(connection, tenant)
            except ServiceError:
                raise failure from None
            row = (
                connection.execute(
                    text(
                        "SELECT id,password_hash,status,session_epoch FROM users "
                        "WHERE tenant_id=:tenant AND username=:username FOR SHARE"
                    ),
                    {"tenant": tenant, "username": username},
                )
                .mappings()
                .first()
            )
        encoded = row["password_hash"] if row and row["status"] == "active" else self._dummy_hash
        try:
            valid = PASSWORD_HASHER.verify(encoded, password)
        except (VerificationError, InvalidHashError, TypeError):
            valid = False
        if not valid or not row or row["status"] != "active":
            raise failure
        principal = load_principal(connection, tenant, row["id"])
        now = utc_now()
        token = secrets.token_urlsafe(32)
        csrf = self._csrf(token)
        session_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO sessions (id,tenant_id,user_id,token_hash,csrf_hash,session_epoch,"
                "expires_at,idle_expires_at) VALUES "
                "(:id,:tenant,:user,:token,:csrf,:epoch,:end,:idle)"
            ),
            {
                "id": session_id,
                "tenant": tenant,
                "user": row["id"],
                "token": token_hash(token),
                "csrf": token_hash(csrf),
                "epoch": row["session_epoch"],
                "end": now + SESSION_ABSOLUTE,
                "idle": now + SESSION_IDLE,
            },
        )
        return AuthenticatedSession(
            session_id, token, csrf, principal, now + SESSION_ABSOLUTE, now + SESSION_IDLE
        )

    def locate(self, connection: Connection, token: str) -> tuple[str, str]:
        """Find only the idempotency scope before row locking; NOT authorization."""
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise authentication_required()
        row = connection.execute(
            text("SELECT tenant_id,user_id FROM sessions WHERE token_hash=:hash"),
            {"hash": token_hash(token)},
        ).first()
        if not row:
            raise authentication_required()
        return row[0], row[1]

    def authenticate(
        self,
        connection: Connection,
        token: str,
        *,
        supplied_csrf: str | None = None,
        touch: bool = True,
        exclusive: bool = False,
    ) -> AuthenticatedSession:
        require_transaction(connection)
        tenant, user = self.locate(connection, token)
        lock_tenant(connection, tenant, exclusive=exclusive)
        principal = load_principal(connection, tenant, user)
        # Tenant -> user/roles -> session, also for login and administrative revocation.
        row = (
            connection.execute(
                text(
                    "SELECT s.id,s.csrf_hash,s.expires_at,s.idle_expires_at,s.revoked_at,"
                    "s.session_epoch,u.session_epoch AS current_epoch FROM sessions s JOIN users u "
                    "ON u.tenant_id=s.tenant_id AND u.id=s.user_id "
                    "WHERE s.tenant_id=:tenant AND s.user_id=:user "
                    "AND s.token_hash=:hash FOR UPDATE"
                ),
                {"tenant": tenant, "user": user, "hash": token_hash(token)},
            )
            .mappings()
            .first()
        )
        now = utc_now()
        if (
            not row
            or row["revoked_at"] is not None
            or row["session_epoch"] != row["current_epoch"]
            or row["expires_at"] <= now
            or row["idle_expires_at"] <= now
        ):
            raise authentication_required()
        csrf = self._csrf(token)
        if not hmac.compare_digest(row["csrf_hash"], token_hash(csrf)):
            raise authentication_required()
        session = AuthenticatedSession(
            row["id"], token, csrf, principal, row["expires_at"], row["idle_expires_at"]
        )
        if supplied_csrf is not None:
            self.require_csrf(session, supplied_csrf)
        if touch:
            idle = min(row["expires_at"], now + SESSION_IDLE)
            connection.execute(
                text(
                    "UPDATE sessions SET idle_expires_at=GREATEST(idle_expires_at,:idle),"
                    "updated_at=:now WHERE tenant_id=:tenant AND id=:id"
                ),
                {"idle": idle, "now": now, "tenant": tenant, "id": row["id"]},
            )
            session = AuthenticatedSession(
                row["id"],
                token,
                csrf,
                principal,
                row["expires_at"],
                max(idle, row["idle_expires_at"]),
            )
        return session

    @staticmethod
    def require_csrf(session: AuthenticatedSession, supplied: str) -> None:
        if (
            not isinstance(supplied, str)
            or not TOKEN_PATTERN.fullmatch(supplied)
            or not hmac.compare_digest(session.csrf_token, supplied)
        ):
            raise ServiceError("CSRF_FAILED", 403, "CSRF validation failed")

    def logout(self, connection: Connection, session: AuthenticatedSession) -> None:
        require_transaction(connection)
        connection.execute(
            text(
                "UPDATE sessions SET revoked_at=:now,updated_at=:now "
                "WHERE tenant_id=:tenant AND user_id=:user AND id=:id AND revoked_at IS NULL"
            ),
            {
                "now": utc_now(),
                "tenant": session.principal.tenant_id,
                "user": session.principal.user_id,
                "id": session.session_id,
            },
        )


def revoke_user_sessions(connection: Connection, tenant_id: str, user_id: str) -> None:
    require_transaction(connection)
    connection.execute(
        text(
            "UPDATE sessions SET revoked_at=:now,updated_at=:now "
            "WHERE tenant_id=:tenant AND user_id=:user AND revoked_at IS NULL"
        ),
        {"now": utc_now(), "tenant": tenant_id, "user": user_id},
    )


def _safe_audit(value: Any) -> Any:
    forbidden = re.compile(r"password|passwd|token|secret|authorization|hash|cookie", re.I)
    if isinstance(value, dict):
        if any(not isinstance(key, str) or forbidden.search(key) for key in value):
            raise ServiceError("VALIDATION_ERROR", 422, "Forbidden audit field")
        return {key: _safe_audit(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_audit(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 4000:
            raise ServiceError("VALIDATION_ERROR", 422, "Audit value too long")
        return value
    raise ServiceError("VALIDATION_ERROR", 422, "Invalid audit value")


def append_audit(
    connection: Connection,
    *,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: str,
    changes: Any,
    reason: str,
    request_id: str,
) -> str:
    require_transaction(connection)
    event_id = str(uuid4())
    values = {
        "id": event_id,
        "tenant": principal.tenant_id,
        "actor": principal.user_id,
        "action": safe_text(action, 100),
        "rtype": safe_text(resource_type, 80),
        "resource": resource_id,
        "changes": canonical_json(_safe_audit(changes)),
        "reason": safe_text(reason, 2000),
        "request": request_id,
    }
    connection.execute(
        text(
            "INSERT INTO audit_events (id,tenant_id,actor_id,action,resource_type,resource_id,"
            "changes,reason,request_id) VALUES "
            "(:id,:tenant,:actor,:action,:rtype,:resource,:changes,:reason,:request)"
        ),
        values,
    )
    return event_id


def begin_idempotency(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    method: str,
    path: str,
    key: str,
    request_hash: str,
    expires_at: datetime,
    lease_owner: str,
) -> IdempotencyClaim:
    require_transaction(connection)
    key = safe_text(key, 128)
    if len(key) < 8 or not re.fullmatch(r"[0-9a-f]{64}", request_hash):
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid idempotency input")
    method = safe_text(method, 10).upper()
    path_hash = hashlib.sha256(safe_text(path, 512).encode()).hexdigest()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    now = utc_now()
    try:
        with connection.begin_nested():
            record_id = str(uuid4())
            connection.execute(
                text(
                    "INSERT INTO api_idempotency (id,tenant_id,actor_id,method,path_hash,key_hash,"
                    "request_hash,state,expires_at,lease_owner,lease_until) VALUES "
                    "(:id,:tenant,:actor,:method,:path,:key,:request,'pending',:expires,:owner,:lease)"
                ),
                {
                    "id": record_id,
                    "tenant": tenant_id,
                    "actor": actor_id,
                    "method": method,
                    "path": path_hash,
                    "key": key_hash,
                    "request": request_hash,
                    "expires": expires_at,
                    "owner": lease_owner,
                    "lease": now + timedelta(seconds=30),
                },
            )
            return IdempotencyClaim(record_id, "pending", lease_owner=lease_owner)
    except IntegrityError as error:
        errno = getattr(getattr(error, "orig", None), "args", [None])[0]
        if errno not in (1062, "1062"):
            raise
    row = (
        connection.execute(
            text(
                "SELECT id,state,request_hash,response_status,response,lease_owner,lease_until "
                "FROM api_idempotency WHERE tenant_id=:tenant AND actor_id=:actor "
                "AND method=:method "
                "AND path_hash=:path AND key_hash=:key AND expires_at>:now"
            ),
            {
                "tenant": tenant_id,
                "actor": actor_id,
                "method": method,
                "path": path_hash,
                "key": key_hash,
                "now": now,
            },
        )
        .mappings()
        .first()
    )
    if not row:
        raise ServiceError(
            "IDEMPOTENCY_EXPIRED", 409, "Idempotency claim expired; retry with a new key"
        )
    if not hmac.compare_digest(row["request_hash"], request_hash):
        raise ServiceError(
            "IDEMPOTENCY_CONFLICT", 409, "Idempotency-Key was reused with a different request"
        )
    response = row["response"]
    if isinstance(response, str):
        response = json.loads(response)
    if row["state"] == "pending":
        if row["lease_until"] is not None and row["lease_until"] <= now:
            updated = connection.execute(
                text(
                    "UPDATE api_idempotency SET lease_owner=:owner,lease_until=:lease "
                    "WHERE id=:id AND state='pending' AND lease_until<=:now"
                ),
                {
                    "owner": lease_owner,
                    "lease": now + timedelta(seconds=30),
                    "id": row["id"],
                    "now": now,
                },
            ).rowcount
            if updated == 1:
                return IdempotencyClaim(row["id"], "pending", lease_owner=lease_owner)
        return IdempotencyClaim(row["id"], "pending", lease_owner=row["lease_owner"])
    return IdempotencyClaim(row["id"], row["state"], row["response_status"], response)


def complete_idempotency(
    connection: Connection,
    claim: IdempotencyClaim,
    *,
    status: int,
    response: Any,
    lease_owner: str | None = None,
) -> None:
    require_transaction(connection)
    owner = lease_owner or claim.lease_owner
    if not owner:
        raise ServiceError("REQUEST_IN_PROGRESS", 409, "Idempotency lease owner is required")
    result = connection.execute(
        text(
            "UPDATE api_idempotency SET state='completed',response_status=:status,"
            "response=:response,lease_owner=NULL,lease_until=NULL "
            "WHERE id=:id AND state='pending' AND lease_owner=:owner"
        ),
        {
            "status": status,
            "response": json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            "id": claim.record_id,
            "owner": owner,
        },
    )
    if result.rowcount != 1:
        raise ServiceError("REQUEST_IN_PROGRESS", 409, "Idempotency lease lost")


class UserSecurityService:
    """Role/status mutations. Caller must commit or roll back this transaction."""

    @staticmethod
    def _lock_user(connection: Connection, tenant_id: str, user_id: str) -> dict[str, Any]:
        row = (
            connection.execute(
                text("SELECT id,status FROM users WHERE tenant_id=:tenant AND id=:user FOR UPDATE"),
                {"tenant": tenant_id, "user": user_id},
            )
            .mappings()
            .first()
        )
        if not row:
            raise ServiceError("NOT_FOUND", 404, "Not found")
        return dict(row)

    @staticmethod
    def _last_admin_guard(connection: Connection, tenant_id: str, user_id: str) -> None:
        active = connection.scalar(
            text(
                "SELECT COUNT(*) FROM users u JOIN user_roles ur ON ur.tenant_id=u.tenant_id "
                "AND ur.user_id=u.id JOIN roles r ON r.id=ur.role_id WHERE u.tenant_id=:tenant "
                "AND u.status='active' AND r.code='safety_admin' AND ur.scope_kind='tenant'"
            ),
            {"tenant": tenant_id},
        )
        mine = connection.scalar(
            text(
                "SELECT COUNT(*) FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
                "WHERE ur.tenant_id=:tenant AND ur.user_id=:user AND r.code='safety_admin' "
                "AND ur.scope_kind='tenant'"
            ),
            {"tenant": tenant_id, "user": user_id},
        )
        if mine and active <= 1:
            raise ServiceError("LAST_ADMIN", 409, "Cannot remove the last safety_admin")

    def disable_user(
        self,
        connection: Connection,
        *,
        tenant_id: str,
        user_id: str,
        actor: Principal,
        request_id: str,
        reason: str,
    ) -> None:
        require_transaction(connection)
        if actor.tenant_id != tenant_id:
            raise ServiceError("NOT_FOUND", 404, "Not found")
        authorize(actor, Permission.ADMIN)
        lock_tenant(connection, tenant_id, exclusive=True)
        user = self._lock_user(connection, tenant_id, user_id)
        if user["status"] == "active":
            self._last_admin_guard(connection, tenant_id, user_id)
        connection.execute(
            text(
                "UPDATE users SET status='disabled',session_epoch=session_epoch+1,"
                "version=version+1,"
                "updated_at=:now WHERE tenant_id=:tenant AND id=:user"
            ),
            {"now": utc_now(), "tenant": tenant_id, "user": user_id},
        )
        revoke_user_sessions(connection, tenant_id, user_id)
        append_audit(
            connection,
            principal=actor,
            action="user.disable",
            resource_type="user",
            resource_id=user_id,
            changes={"status": "disabled"},
            reason=reason,
            request_id=request_id,
        )

    def revoke_safety_admin(
        self,
        connection: Connection,
        *,
        tenant_id: str,
        user_id: str,
        role_id: str,
        actor: Principal,
        request_id: str,
        reason: str,
    ) -> None:
        require_transaction(connection)
        if actor.tenant_id != tenant_id:
            raise ServiceError("NOT_FOUND", 404, "Not found")
        authorize(actor, Permission.ADMIN)
        lock_tenant(connection, tenant_id, exclusive=True)
        self._lock_user(connection, tenant_id, user_id)
        self._last_admin_guard(connection, tenant_id, user_id)
        deleted = connection.execute(
            text(
                "DELETE ur FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
                "WHERE ur.tenant_id=:tenant "
                "AND ur.user_id=:user AND ur.role_id=:role AND r.code='safety_admin'"
            ),
            {"tenant": tenant_id, "user": user_id, "role": role_id},
        ).rowcount
        if deleted != 1:
            raise ServiceError("NOT_FOUND", 404, "Not found")
        connection.execute(
            text(
                "UPDATE users SET session_epoch=session_epoch+1,version=version+1,updated_at=:now "
                "WHERE tenant_id=:tenant AND id=:user"
            ),
            {"now": utc_now(), "tenant": tenant_id, "user": user_id},
        )
        revoke_user_sessions(connection, tenant_id, user_id)
        append_audit(
            connection,
            principal=actor,
            action="role.revoke",
            resource_type="user_role",
            resource_id=user_id,
            changes={"role": "safety_admin"},
            reason=reason,
            request_id=request_id,
        )
