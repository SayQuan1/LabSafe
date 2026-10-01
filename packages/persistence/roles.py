"""Tenant-scoped role assignments with user versioning and atomic revocation."""

from uuid import uuid4

from sqlalchemy import text

from packages.domain.security import (
    Permission,
    ServiceError,
    authorize,
    not_found,
    permission_projection,
    require_version,
    valid_grant,
)
from packages.persistence.security import (
    UserSecurityService,
    append_audit,
    load_principal,
    revoke_user_sessions,
)
from packages.persistence.transaction import lock_tenant, require_transaction, utc_now
from packages.persistence.users import UserRepository, timestamp


class RoleRepository:
    @staticmethod
    def laboratory(connection, tenant, laboratory_id):
        row = connection.execute(
            text("SELECT status FROM laboratories WHERE tenant_id=:tenant AND id=:id FOR SHARE"),
            {"tenant": tenant, "id": laboratory_id},
        ).first()
        if row is None:
            raise not_found()
        return row.status

    def list(self, connection, tenant, user_id, page, page_size, laboratory_id=None):
        require_transaction(connection)
        UserRepository().get(connection, tenant, user_id)
        if laboratory_id is not None:
            self.laboratory(connection, tenant, laboratory_id)
        where = "ur.tenant_id=:tenant AND ur.user_id=:user"
        if laboratory_id is not None:
            where += " AND ur.laboratory_id=:lab"
        params = {
            "tenant": tenant,
            "user": user_id,
            "lab": laboratory_id,
            "limit": page_size,
            "offset": (page - 1) * page_size,
        }
        total = connection.scalar(text(f"SELECT COUNT(*) FROM user_roles ur WHERE {where}"), params)
        rows = connection.execute(
            text(
                "SELECT ur.id,ur.created_at,ur.updated_at,ur.version,ur.user_id,r.code AS role,"
                "ur.scope_kind,ur.laboratory_id FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
                f"WHERE {where} ORDER BY ur.created_at DESC,ur.id DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        ).mappings()
        items = []
        for row in rows:
            item = dict(row)
            for field in ("created_at", "updated_at"):
                item[field] = timestamp(item[field])
            items.append(item)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def change(self, connection, actor, user_id, body, request_id, *, grant):
        require_transaction(connection)
        authorize(actor, Permission.ADMIN)
        tenant = actor.tenant_id
        # Application must take this lock before the idempotency INSERT's FK locks.
        lock_tenant(connection, tenant, exclusive=True)
        actor = load_principal(connection, tenant, actor.user_id)
        authorize(actor, Permission.ADMIN)
        users = UserRepository()
        user = users.get(connection, tenant, user_id, lock=True)
        require_version(user["version"], body["expected_version"])
        role, scope, lab = body["role"], body["scope_kind"], body["laboratory_id"]
        if not valid_grant(role, scope, lab):
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid role scope")
        if grant and user["status"] != "active":
            raise ServiceError("STATE_CONFLICT", 409, "Cannot grant roles to a disabled user")
        if lab is not None:
            status = self.laboratory(connection, tenant, lab)
            if grant and status != "active":
                raise ServiceError("STATE_CONFLICT", 409, "Cannot grant roles in an archived lab")
        role_id = connection.scalar(
            text("SELECT id FROM roles WHERE code=:role FOR SHARE"), {"role": role}
        )
        if role_id is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Role catalog is not ready")
        params = {"tenant": tenant, "user": user_id, "role": role_id, "scope": scope, "lab": lab}
        assignment = connection.scalar(
            text(
                "SELECT id FROM user_roles WHERE tenant_id=:tenant AND user_id=:user "
                "AND role_id=:role AND scope_kind=:scope AND laboratory_id <=> :lab FOR UPDATE"
            ),
            params,
        )
        if grant:
            if assignment is not None:
                raise ServiceError("STATE_CONFLICT", 409, "Role assignment already exists")
            assignment = str(uuid4())
            connection.execute(
                text(
                    "INSERT INTO user_roles "
                    "(id,tenant_id,user_id,role_id,scope_kind,laboratory_id) "
                    "VALUES (:id,:tenant,:user,:role,:scope,:lab)"
                ),
                {**params, "id": assignment},
            )
            # A grant must not make the next login exceed Session.permissions capacity.
            permission_projection(load_principal(connection, tenant, user_id))
        else:
            if assignment is None:
                raise not_found()
            if role == "safety_admin" and user["status"] == "active":
                UserSecurityService()._last_admin_guard(connection, tenant, user_id)
            connection.execute(
                text("DELETE FROM user_roles WHERE tenant_id=:tenant AND user_id=:user AND id=:id"),
                {"tenant": tenant, "user": user_id, "id": assignment},
            )
        connection.execute(
            text(
                "UPDATE users SET version=version+1,session_epoch=session_epoch+1,updated_at=:now "
                "WHERE tenant_id=:tenant AND id=:user"
            ),
            {"tenant": tenant, "user": user_id, "now": utc_now()},
        )
        revoke_user_sessions(connection, tenant, user_id)
        append_audit(
            connection,
            principal=actor,
            action="role.grant" if grant else "role.revoke",
            resource_type="user_role",
            resource_id=assignment,
            changes={"user_id": user_id, "role": role, "scope_kind": scope, "laboratory_id": lab},
            reason="Grant role" if grant else "Revoke role",
            request_id=request_id,
        )
        return users.get(connection, tenant, user_id, lock=True)
