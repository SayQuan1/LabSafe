"""Tenant-scoped user repository; all projections exclude authentication secrets."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from packages.domain.security import ServiceError, not_found, require_version
from packages.persistence.bootstrap import PASSWORD_HASHER, normalized
from packages.persistence.security import UserSecurityService, append_audit

USER_COLUMNS = "id,created_at,updated_at,version,username,display_name,status"


def timestamp(value: datetime) -> str:
    return (
        value.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def project_user(row):
    result = dict(row)
    for field in ("created_at", "updated_at"):
        result[field] = timestamp(result[field])
    return result


class UserRepository:
    def get(self, connection, tenant, user_id, *, lock=False):
        suffix = " FOR UPDATE" if lock else ""
        row = (
            connection.execute(
                text(
                    f"SELECT {USER_COLUMNS} FROM users WHERE tenant_id=:tenant AND id=:id{suffix}"
                ),
                {"tenant": tenant, "id": user_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        return project_user(row)

    def list(self, connection, tenant, page, page_size):
        # Both nonlocking reads share the transaction's REPEATABLE READ snapshot.
        total = connection.scalar(
            text("SELECT COUNT(*) FROM users WHERE tenant_id=:tenant"), {"tenant": tenant}
        )
        rows = connection.execute(
            text(
                f"SELECT {USER_COLUMNS} FROM users WHERE tenant_id=:tenant "
                "ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"
            ),
            {"tenant": tenant, "limit": page_size, "offset": (page - 1) * page_size},
        ).mappings()
        return {
            "items": [project_user(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def create(self, connection, actor, body, request_id):
        try:
            username = normalized(body["username"], 128, account=True)
            display = normalized(body["display_name"], 100)
        except (ValueError, TypeError):
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid user details") from None
        password = body["initial_password"]
        if not 12 <= len(password) <= 128:
            raise ServiceError(
                "VALIDATION_ERROR", 422, "Password must contain 12 to 128 characters"
            )
        user_id = str(uuid4())
        try:
            connection.execute(
                text(
                    "INSERT INTO users (id,tenant_id,username,display_name,password_hash) "
                    "VALUES (:id,:tenant,:username,:display,:encoded)"
                ),
                {
                    "id": user_id,
                    "tenant": actor.tenant_id,
                    "username": username,
                    "display": display,
                    "encoded": PASSWORD_HASHER.hash(password),
                },
            )
        except IntegrityError as error:
            if getattr(error.orig, "args", [None])[0] == 1062:
                raise ServiceError("STATE_CONFLICT", 409, "Username already exists") from None
            raise
        append_audit(
            connection,
            principal=actor,
            action="user.create",
            resource_type="user",
            resource_id=user_id,
            changes={"status": "active"},
            reason="Create user",
            request_id=request_id,
        )
        return self.get(connection, actor.tenant_id, user_id, lock=True)

    def disable(self, connection, actor, user_id, body, request_id):
        user = self.get(connection, actor.tenant_id, user_id, lock=True)
        require_version(user["version"], body["expected_version"])
        if user["status"] != "active":
            raise ServiceError("STATE_CONFLICT", 409, "User is already disabled")
        UserSecurityService().disable_user(
            connection,
            tenant_id=actor.tenant_id,
            user_id=user_id,
            actor=actor,
            request_id=request_id,
            reason=body["reason"],
        )
        return self.get(connection, actor.tenant_id, user_id, lock=True)
