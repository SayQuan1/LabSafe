"""Immutable published template bodies with serialized family revision allocation."""

from uuid import uuid4

from sqlalchemy import text

from packages.domain.foundations import template_items
from packages.domain.security import Permission, ServiceError, authorize, not_found, require_version
from packages.persistence.foundations import BASE, clean, is_admin, page_query, project
from packages.persistence.security import append_audit
from packages.persistence.transaction import utc_now

COLUMNS = BASE + ",name,revision,status"


class TemplateRepository:
    @staticmethod
    def row(connection, tenant, template_id, *, lock=False, share=False):
        row = (
            connection.execute(
                text(
                    f"SELECT {COLUMNS},family_id FROM inspection_templates "
                    "WHERE tenant_id=:tenant AND id=:id"
                    + (" FOR UPDATE" if lock else " FOR SHARE" if share else "")
                ),
                {"tenant": tenant, "id": template_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        return row

    @staticmethod
    def items(connection, tenant, template_id, *, share=False):
        rows = (
            connection.execute(
                text(
                    "SELECT id,code,title,capture_hint,sort_order,required FROM template_items "
                    "WHERE tenant_id=:tenant AND template_id=:id ORDER BY sort_order,id"
                    + (" FOR SHARE" if share else "")
                ),
                {"tenant": tenant, "id": template_id},
            )
            .mappings()
            .all()
        )
        return [{**row, "required": bool(row["required"])} for row in rows]

    def project(self, connection, tenant, row, *, share=False):
        result = project({key: row[key] for key in COLUMNS.split(",")})
        result["items"] = self.items(connection, tenant, row["id"], share=share)
        template_items(result["items"])
        return result

    def get(self, connection, actor, template_id, *, share=False):
        authorize(actor, Permission.READ, published=True)
        row = self.row(connection, actor.tenant_id, template_id, share=share)
        if row["status"] != "published" and not is_admin(actor):
            raise not_found()
        return self.project(connection, actor.tenant_id, row, share=share)

    def list(self, connection, actor, page, page_size):
        authorize(actor, Permission.READ, published=True)
        where = "tenant_id=:tenant" + ("" if is_admin(actor) else " AND status='published'")
        return page_query(
            connection,
            "inspection_templates",
            COLUMNS,
            where,
            {"tenant": actor.tenant_id},
            page,
            page_size,
            project_row=lambda row: self.project(connection, actor.tenant_id, row),
        )

    def insert(
        self,
        connection,
        actor,
        name,
        items,
        request_id,
        *,
        family=None,
        revision=1,
        reason="Create template",
    ):
        items = [
            {
                **item,
                "code": clean(item["code"], 64),
                "title": clean(item["title"], 200),
                "capture_hint": clean(item["capture_hint"], 1000),
            }
            for item in items
        ]
        template_items(items)
        template_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO inspection_templates (id,tenant_id,family_id,revision,name) "
                "VALUES (:id,:tenant,:family,:revision,:name)"
            ),
            {
                "id": template_id,
                "tenant": actor.tenant_id,
                "family": family or template_id,
                "revision": revision,
                "name": clean(name, 200),
            },
        )
        connection.execute(
            text(
                "INSERT INTO template_items "
                "(id,tenant_id,template_id,code,title,capture_hint,sort_order,required) "
                "VALUES (:id,:tenant,:template,:code,:title,:capture_hint,:sort_order,:required)"
            ),
            [{**item, "tenant": actor.tenant_id, "template": template_id} for item in items],
        )
        append_audit(
            connection,
            principal=actor,
            action="template.clone" if family else "template.create",
            resource_type="template",
            resource_id=template_id,
            changes={"revision": revision},
            reason=reason,
            request_id=request_id,
        )
        return self.get(connection, actor, template_id)

    def create(self, connection, actor, body, request_id):
        authorize(actor, Permission.ADMIN)
        return self.insert(connection, actor, body["name"], body["items"], request_id)

    def change(self, connection, actor, template_id, body, request_id, *, clone):
        authorize(actor, Permission.ADMIN)
        if clone:
            source = self.row(connection, actor.tenant_id, template_id)
            # Every family root is a durable mutex, also when cloning a later revision.
            self.row(connection, actor.tenant_id, source["family_id"], lock=True)
        source = self.row(connection, actor.tenant_id, template_id, lock=True)
        require_version(source["version"], body["expected_version"])
        items = self.items(connection, actor.tenant_id, template_id)
        template_items(items)
        if clone:
            revisions = (
                connection.execute(
                    text(
                        "SELECT revision FROM inspection_templates "
                        "WHERE tenant_id=:tenant AND family_id=:family ORDER BY revision FOR UPDATE"
                    ),
                    {"tenant": actor.tenant_id, "family": source["family_id"]},
                )
                .scalars()
                .all()
            )
            if max(revisions) >= 2147483647:
                raise ServiceError("STATE_CONFLICT", 409, "Template revision exceeds capacity")
            # The selected source stays immutable; allocate after latest committed family revision.
            return self.insert(
                connection,
                actor,
                source["name"],
                [{**i, "id": str(uuid4())} for i in items],
                request_id,
                family=source["family_id"],
                revision=max(revisions) + 1,
                reason=body["reason"],
            )
        if source["status"] != "draft":
            raise ServiceError("STATE_CONFLICT", 409, "Only a draft template can be published")
        connection.execute(
            text(
                "UPDATE inspection_templates "
                "SET status='published',version=version+1,updated_at=:now "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": actor.tenant_id, "id": template_id, "now": utc_now()},
        )
        append_audit(
            connection,
            principal=actor,
            action="template.publish",
            resource_type="template",
            resource_id=template_id,
            changes={"status": "published"},
            reason=body["reason"],
            request_id=request_id,
        )
        return self.get(connection, actor, template_id)
