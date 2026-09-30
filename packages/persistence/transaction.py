"""Explicit transaction and tenant-lock primitives for application services."""

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from packages.domain.security import ServiceError


def utc_now() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def require_transaction(connection: Connection) -> None:
    if not connection.in_transaction():
        raise RuntimeError("An explicit application transaction is required")


def lock_tenant(connection: Connection, tenant_id: str, *, exclusive: bool = False) -> None:
    require_transaction(connection)
    lock = "FOR UPDATE" if exclusive else "FOR SHARE"
    status = connection.scalar(
        text(f"SELECT status FROM tenants WHERE id=:tenant {lock}"), {"tenant": tenant_id}
    )
    if status != "active":
        raise ServiceError("AUTHENTICATION_REQUIRED", 401, "Authentication required")
