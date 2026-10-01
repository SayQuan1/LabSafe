"""Scoped organization read models and append-only creation commands."""

from uuid import uuid4

from sqlalchemy import text

from packages.domain.foundations import location_parent, require_active
from packages.domain.security import Permission, authorize, not_found
from packages.persistence.foundations import BASE, clean, is_admin, lab_filter, page_query, project
from packages.persistence.security import append_audit

TABLES = {
    "college": ("colleges", BASE + ",name,code,status"),
    "laboratory": ("laboratories", BASE + ",college_id,name,code,status"),
    "location": ("locations", BASE + ",laboratory_id,parent_id,type,label,status"),
}


class OrganizationRepository:
    def row(self, connection, tenant, kind, resource_id, *, laboratory_id=None):
        table, columns = TABLES[kind]
        where = "tenant_id=:tenant AND id=:id"
        if laboratory_id is not None:
            where += " AND laboratory_id=:lab"
        row = (
            connection.execute(
                text(f"SELECT {columns} FROM {table} WHERE {where} FOR SHARE"),
                {"tenant": tenant, "id": resource_id, "lab": laboratory_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        return row

    def get(self, connection, actor, kind, resource_id):
        row = self.row(connection, actor.tenant_id, kind, resource_id)
        if kind == "college":
            authorize(actor, Permission.READ, published=True)
            if not is_admin(actor) and row["status"] != "active":
                raise not_found()
        else:
            authorize(actor, Permission.READ, resource_id)
        return project(row)

    def list(self, connection, actor, kind, page, page_size, laboratory_id=None):
        table, columns = TABLES[kind]
        params = {"tenant": actor.tenant_id}
        where = "tenant_id=:tenant"
        if kind == "college":
            authorize(actor, Permission.READ, published=True)
            if not is_admin(actor):
                where += " AND status='active'"
        else:
            if laboratory_id is not None:
                self.get(connection, actor, "laboratory", laboratory_id)
            where += lab_filter(
                actor, "id" if kind == "laboratory" else "laboratory_id", params, laboratory_id
            )
        return page_query(connection, table, columns, where, params, page, page_size)

    def create(self, connection, actor, kind, body, request_id, *, laboratory_id=None):
        authorize(actor, Permission.ADMIN)
        tenant, resource_id = actor.tenant_id, str(uuid4())
        if kind in {"college", "laboratory"}:
            params = {
                "id": resource_id,
                "tenant": tenant,
                "code": clean(body["code"], 64),
                "name": clean(body["name"], 200),
            }
            if kind == "college":
                statement = (
                    "INSERT INTO colleges (id,tenant_id,code,name) VALUES (:id,:tenant,:code,:name)"
                )
            else:
                college = self.row(connection, tenant, "college", body["college_id"])
                require_active(college["status"])
                params["college"] = college["id"]
                statement = (
                    "INSERT INTO laboratories (id,tenant_id,college_id,code,name) "
                    "VALUES (:id,:tenant,:college,:code,:name)"
                )
        else:
            lab = self.row(connection, tenant, "laboratory", laboratory_id)
            require_active(lab["status"])
            parent = None
            if body["parent_id"] is not None:
                parent = self.row(
                    connection, tenant, "location", body["parent_id"], laboratory_id=laboratory_id
                )
                require_active(parent["status"])
            location_parent(body["type"], parent["type"] if parent else None)
            # New server ID + existing legal parent type makes a cycle impossible.
            params = {
                "id": resource_id,
                "tenant": tenant,
                "lab": laboratory_id,
                "parent": body["parent_id"],
                "kind": body["type"],
                "label": clean(body["label"], 200),
            }
            statement = (
                "INSERT INTO locations (id,tenant_id,laboratory_id,parent_id,type,label) "
                "VALUES (:id,:tenant,:lab,:parent,:kind,:label)"
            )
        connection.execute(text(statement), params)
        append_audit(
            connection,
            principal=actor,
            action=f"{kind}.create",
            resource_type=kind,
            resource_id=resource_id,
            changes={"status": "active"},
            reason=f"Create {kind}",
            request_id=request_id,
        )
        return project(self.row(connection, tenant, kind, resource_id))
