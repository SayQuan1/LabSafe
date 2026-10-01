"""Inspection drafts pin published templates and create all items atomically."""

from uuid import uuid4

from sqlalchemy import text

from packages.domain.foundations import create_inspection, require_active
from packages.domain.security import Permission, authorize, not_found
from packages.persistence.foundations import BASE, lab_filter, page_query, project
from packages.persistence.organizations import OrganizationRepository
from packages.persistence.security import append_audit
from packages.persistence.templates import TemplateRepository

INSPECTION = BASE + ",laboratory_id,template_id,inspector_id,status"


class InspectionRepository:
    @staticmethod
    def project(connection, tenant, row, *, share=False):
        result = project(row)
        result["item_ids"] = list(
            connection.execute(
                text(
                    "SELECT id FROM inspection_items WHERE tenant_id=:tenant "
                    "AND inspection_id=:id ORDER BY created_at,id" + (" FOR SHARE" if share else "")
                ),
                {"tenant": tenant, "id": row["id"]},
            ).scalars()
        )
        return result

    def get(self, connection, actor, inspection_id, *, share=False):
        row = (
            connection.execute(
                text(
                    f"SELECT {INSPECTION} FROM inspections WHERE tenant_id=:tenant AND id=:id"
                    + (" FOR SHARE" if share else "")
                ),
                {"tenant": actor.tenant_id, "id": inspection_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        authorize(actor, Permission.READ, row["laboratory_id"])
        return self.project(connection, actor.tenant_id, row, share=share)

    def list(self, connection, actor, page, page_size, laboratory_id=None):
        params = {"tenant": actor.tenant_id}
        if laboratory_id is not None:
            OrganizationRepository().get(connection, actor, "laboratory", laboratory_id)
        where = "tenant_id=:tenant" + lab_filter(actor, "laboratory_id", params, laboratory_id)
        return page_query(
            connection,
            "inspections",
            INSPECTION,
            where,
            params,
            page,
            page_size,
            project_row=lambda row: self.project(connection, actor.tenant_id, row),
        )

    def create(self, connection, actor, body, request_id):
        lab_id, template_id = body["laboratory_id"], body["template_id"]
        authorize(actor, Permission.CAPTURE, lab_id)
        organizations, templates = OrganizationRepository(), TemplateRepository()
        lab = organizations.row(connection, actor.tenant_id, "laboratory", lab_id)
        require_active(lab["status"])
        # Shared lock is enough: published bodies are immutable and retained.
        template = templates.get(connection, actor, template_id, share=True)
        create_inspection(
            actor, lab_id, template["status"], len(template["items"]), body["location_ids"]
        )
        for location_id in sorted(body["location_ids"]):
            location = organizations.row(
                connection, actor.tenant_id, "location", location_id, laboratory_id=lab_id
            )
            require_active(location["status"])
        inspection_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO inspections (id,tenant_id,laboratory_id,template_id,inspector_id) "
                "VALUES (:id,:tenant,:lab,:template,:actor)"
            ),
            {
                "id": inspection_id,
                "tenant": actor.tenant_id,
                "lab": lab_id,
                "template": template_id,
                "actor": actor.user_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO inspection_items "
                "(id,tenant_id,inspection_id,laboratory_id,template_item_id,location_id) "
                "VALUES (:id,:tenant,:inspection,:lab,:item,:location)"
            ),
            [
                {
                    "id": str(uuid4()),
                    "tenant": actor.tenant_id,
                    "inspection": inspection_id,
                    "lab": lab_id,
                    "item": item["id"],
                    "location": location,
                }
                for item in template["items"]
                for location in body["location_ids"]
            ],
        )
        append_audit(
            connection,
            principal=actor,
            action="inspection.create",
            resource_type="inspection",
            resource_id=inspection_id,
            changes={"status": "draft", "template_id": template_id},
            reason="Create inspection",
            request_id=request_id,
        )
        return self.get(connection, actor, inspection_id)
