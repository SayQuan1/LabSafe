"""One-time tenant/admin creation; all records and audit commit atomically."""

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from argon2 import PasswordHasher, Type
from sqlalchemy import text
from sqlalchemy.engine import Engine

from packages.persistence.database import DatabaseSafetyError
from packages.persistence.schema import verify_schema

ROLES = {
    "safety_admin": "安全管理员",
    "lab_manager": "实验室负责人",
    "inspector": "巡检员",
    "remediator": "整改员",
    "viewer": "只读用户",
    "rule_expert": "规则专家",
}
PASSWORD_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16, type=Type.ID
)


def normalized(value: str, limit: int, *, account: bool = False) -> str:
    value = unicodedata.normalize("NFKC" if account else "NFC", value).strip()
    if account:
        value = value.casefold()
    if not 1 <= len(value) <= limit or any(unicodedata.category(c).startswith("C") for c in value):
        raise DatabaseSafetyError("Invalid text length or control characters in bootstrap input")
    return value


@dataclass(frozen=True)
class TenantInput:
    code: str
    name: str
    timezone: str
    username: str
    display_name: str
    reason: str

    def validated(self):
        code = normalized(self.code, 64)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", code):
            raise DatabaseSafetyError(
                "Tenant code must use lowercase ASCII letters, digits, _ or -"
            )
        timezone = normalized(self.timezone, 64)
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise DatabaseSafetyError("Unknown IANA timezone") from None
        return TenantInput(
            code,
            normalized(self.name, 200),
            timezone,
            normalized(self.username, 128, account=True),
            normalized(self.display_name, 100),
            normalized(self.reason, 2000),
        )


def bootstrap_tenant(engine: Engine, inputs: TenantInput, password: str) -> dict[str, str]:
    inputs = inputs.validated()
    # Passwords are never normalized, stripped, truncated, logged or returned.
    if not 12 <= len(password) <= 128:
        raise DatabaseSafetyError("Password must contain 12 to 128 characters")
    password_hash = PASSWORD_HASHER.hash(password)
    tenant_id, user_id = str(uuid4()), str(uuid4())
    lock = hashlib.sha256(f"labsafe:bootstrap:{engine.url.database}".encode()).hexdigest()
    with engine.connect() as connection:
        if connection.scalar(text("SELECT GET_LOCK(:name, 10)"), {"name": lock}) != 1:
            raise DatabaseSafetyError("Another bootstrap owns the database lock")
        connection.commit()
        try:
            with connection.begin():
                if verify_schema(connection)["errors"]:
                    raise DatabaseSafetyError("Bootstrap requires verified migration 0001_initial")
                if connection.scalar(
                    text("SELECT id FROM tenants WHERE code=:code"), {"code": inputs.code}
                ):
                    raise DatabaseSafetyError(
                        "Tenant already exists; bootstrap never resets accounts"
                    )
                existing = dict(connection.execute(text("SELECT code,id FROM roles")).all())
                if set(existing) - set(ROLES):
                    raise DatabaseSafetyError(
                        "Unknown roles found; resolve schema/seed drift first"
                    )
                for code, name in ROLES.items():
                    if code not in existing:
                        role_id = str(uuid5(NAMESPACE_URL, f"urn:labsafe:role:{code}"))
                        connection.execute(
                            text("INSERT INTO roles (id,code,name) VALUES (:id,:code,:name)"),
                            {"id": role_id, "code": code, "name": name},
                        )
                        existing[code] = role_id
                connection.execute(
                    text(
                        "INSERT INTO tenants (id,code,name,timezone) "
                        "VALUES (:id,:code,:name,:timezone)"
                    ),
                    {
                        "id": tenant_id,
                        "code": inputs.code,
                        "name": inputs.name,
                        "timezone": inputs.timezone,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO users (id,tenant_id,username,display_name,password_hash) "
                        "VALUES (:id,:tenant,:username,:display_name,:password_hash)"
                    ),
                    {
                        "id": user_id,
                        "tenant": tenant_id,
                        "username": inputs.username,
                        "display_name": inputs.display_name,
                        "password_hash": password_hash,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO user_roles (id,tenant_id,user_id,role_id,scope_kind) "
                        "VALUES (:id,:tenant,:user,:role,'tenant')"
                    ),
                    {
                        "id": str(uuid4()),
                        "tenant": tenant_id,
                        "user": user_id,
                        "role": existing["safety_admin"],
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO audit_events "
                        "(id,tenant_id,actor_id,action,resource_type,resource_id,"
                        "changes,reason,request_id) "
                        "VALUES (:id,:tenant,:actor,'tenant.bootstrap','tenant',:tenant,"
                        ":changes,:reason,:request)"
                    ),
                    {
                        "id": str(uuid4()),
                        "tenant": tenant_id,
                        "actor": user_id,
                        "changes": json.dumps(
                            {
                                "initial_admin_id": user_id,
                                "role_codes": list(ROLES),
                                "scope_kind": "tenant",
                            }
                        ),
                        "reason": inputs.reason,
                        "request": str(uuid4()),
                    },
                )
        finally:
            connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock})
    return {"tenant_id": tenant_id, "admin_id": user_id, "role": "safety_admin"}
