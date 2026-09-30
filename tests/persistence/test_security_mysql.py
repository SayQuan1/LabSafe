import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from packages.domain.security import Principal, ServiceError
from packages.persistence.bootstrap import TenantInput, bootstrap_tenant
from packages.persistence.security import (
    SESSION_ABSOLUTE,
    SESSION_IDLE,
    SessionService,
    UserSecurityService,
    begin_idempotency,
    complete_idempotency,
    revoke_user_sessions,
    utc_now,
)

PASSWORD = "I-01C-security-password"


def create_tenant(engine, suffix: str):
    data = TenantInput(
        f"sec_{suffix}_{uuid4().hex[:8]}",
        "安全测试机构",
        "Asia/Shanghai",
        "Admin",
        "管理员",
        "I-01C security test",
    )
    result = bootstrap_tenant(engine, data, PASSWORD)
    result["code"] = data.code
    return result


def principal_for(connection, tenant_id, user_id):
    row = connection.execute(
        text("SELECT username FROM users WHERE tenant_id=:tenant AND id=:user"),
        {"tenant": tenant_id, "user": user_id},
    ).scalar_one()
    return Principal(user_id, tenant_id, row, (("safety_admin", "tenant", None),))


def test_session_login_csrf_logout_and_epoch_revocation(database):
    result = create_tenant(database, "session")
    service = SessionService(b"c" * 32)
    with database.connect() as connection:
        with connection.begin():
            session = service.login(
                connection, tenant_code=result["code"], username="ADMIN", password=PASSWORD
            )
            # The generated code is not known from the helper; use the actual tenant lookup.
            assert session.principal.user_id == result["admin_id"]
            authenticated = service.authenticate(
                connection, session.token, supplied_csrf=session.csrf_token
            )
            assert authenticated.principal.tenant_id == result["tenant_id"]
            with pytest.raises(ServiceError) as error:
                service.authenticate(connection, session.token, supplied_csrf="A" * 43)
            assert error.value.status == 403
            service.logout(connection, session)
            with pytest.raises(ServiceError) as error:
                service.authenticate(connection, session.token, touch=False)
            assert error.value.status == 401

    with database.connect() as connection:
        with connection.begin():
            session = service.login(
                connection,
                tenant_code=connection.scalar(
                    text("SELECT code FROM tenants WHERE id=:id"), {"id": result["tenant_id"]}
                ),
                username="admin",
                password=PASSWORD,
            )
            connection.execute(
                text(
                    "UPDATE users SET session_epoch=session_epoch+1 "
                    "WHERE tenant_id=:tenant AND id=:user"
                ),
                {"tenant": result["tenant_id"], "user": result["admin_id"]},
            )
            revoke_user_sessions(connection, result["tenant_id"], result["admin_id"])
            with pytest.raises(ServiceError):
                service.authenticate(connection, session.token, touch=False)
    assert SESSION_ABSOLUTE > SESSION_IDLE


def test_idempotency_replay_conflict_and_lease_fencing(database):
    result = create_tenant(database, "idem")
    request_hash = hashlib.sha256(b"canonical-request").hexdigest()
    with database.connect() as connection:
        with connection.begin():
            now = utc_now()
            first = begin_idempotency(
                connection,
                tenant_id=result["tenant_id"],
                actor_id=result["admin_id"],
                method="POST",
                path="/api/v1/x",
                key="idem-key-1",
                request_hash=request_hash,
                expires_at=now + timedelta(hours=24),
                lease_owner="owner-a",
            )
            complete_idempotency(connection, first, status=201, response={"ok": True})
            replay = begin_idempotency(
                connection,
                tenant_id=result["tenant_id"],
                actor_id=result["admin_id"],
                method="POST",
                path="/api/v1/x",
                key="idem-key-1",
                request_hash=request_hash,
                expires_at=now + timedelta(hours=24),
                lease_owner="owner-b",
            )
            assert replay.state == "completed" and replay.response == {"ok": True}
            with pytest.raises(ServiceError) as error:
                begin_idempotency(
                    connection,
                    tenant_id=result["tenant_id"],
                    actor_id=result["admin_id"],
                    method="POST",
                    path="/api/v1/x",
                    key="idem-key-1",
                    request_hash=hashlib.sha256(b"different").hexdigest(),
                    expires_at=now + timedelta(hours=24),
                    lease_owner="owner-c",
                )
            assert error.value.code == "IDEMPOTENCY_CONFLICT"

            pending = begin_idempotency(
                connection,
                tenant_id=result["tenant_id"],
                actor_id=result["admin_id"],
                method="POST",
                path="/api/v1/pending",
                key="idem-key-2",
                request_hash=request_hash,
                expires_at=now + timedelta(hours=24),
                lease_owner="owner-a",
            )
            with pytest.raises(ServiceError):
                complete_idempotency(
                    connection, pending, status=200, response={}, lease_owner="owner-old"
                )
            connection.execute(
                text("UPDATE api_idempotency SET lease_until=:expired WHERE id=:id"),
                {"expired": now - timedelta(seconds=1), "id": pending.record_id},
            )
            takeover = begin_idempotency(
                connection,
                tenant_id=result["tenant_id"],
                actor_id=result["admin_id"],
                method="POST",
                path="/api/v1/pending",
                key="idem-key-2",
                request_hash=request_hash,
                expires_at=now + timedelta(hours=24),
                lease_owner="owner-new",
            )
            complete_idempotency(connection, takeover, status=200, response={"taken": True})


def test_last_admin_and_cross_tenant_protection(database):
    one = create_tenant(database, "adminone")
    two = create_tenant(database, "admintwo")
    service = UserSecurityService()
    with database.connect() as connection:
        with connection.begin():
            actor = principal_for(connection, one["tenant_id"], one["admin_id"])
            with pytest.raises(ServiceError) as error:
                service.disable_user(
                    connection,
                    tenant_id=one["tenant_id"],
                    user_id=one["admin_id"],
                    actor=actor,
                    request_id=str(uuid4()),
                    reason="test",
                )
            assert error.value.code == "LAST_ADMIN"
            with pytest.raises(ServiceError) as error:
                service.disable_user(
                    connection,
                    tenant_id=two["tenant_id"],
                    user_id=two["admin_id"],
                    actor=actor,
                    request_id=str(uuid4()),
                    reason="cross tenant",
                )
            assert error.value.code == "NOT_FOUND"
