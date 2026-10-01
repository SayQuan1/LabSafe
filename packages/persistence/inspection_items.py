"""Tenant-scoped, snapshot-consistent inspection item read models."""

from sqlalchemy import text

from packages.domain.item_actions import ItemActionContext, allowed_item_actions
from packages.domain.security import Permission, authorize, not_found
from packages.domain.workflow import Item
from packages.persistence.foundations import project

PUBLIC_FIELDS = (
    "id",
    "created_at",
    "updated_at",
    "version",
    "inspection_id",
    "laboratory_id",
    "location_id",
    "status",
    "submission_revision",
    "current_run_id",
    "current_fact_revision_id",
    "review_outcome",
)
FIELDS = ",".join("item." + name for name in PUBLIC_FIELDS)
INTERNAL_FIELDS = (
    ",item.tenant_id,item.current_evaluation_id,inspection.status AS inspection_status,"
    "inspection.inspector_id AS creator_id"
)
FROM = (
    "inspection_items item JOIN inspections inspection "
    "ON inspection.tenant_id=item.tenant_id AND inspection.id=item.inspection_id "
    "AND inspection.laboratory_id=item.laboratory_id"
)


class InspectionItemRepository:
    @staticmethod
    def project(actor, row):
        snapshot = Item(**{name: row[name] for name in Item.__dataclass_fields__})
        result = project({name: row[name] for name in PUBLIC_FIELDS})
        # No item write command is mounted in I-02E. Enabling one requires its
        # real handler and a complete trusted context loader in the same change.
        # Do not infer images/evaluation/U from status or manufacture empty facts.
        result["allowed_actions"] = allowed_item_actions(
            actor, snapshot, ItemActionContext(), enabled=frozenset()
        )
        return result

    def get(self, connection, actor, item_id):
        row = (
            connection.execute(
                text(
                    f"SELECT {FIELDS}{INTERNAL_FIELDS} FROM {FROM} "
                    "WHERE item.tenant_id=:tenant AND item.id=:id"
                ),
                {"tenant": actor.tenant_id, "id": item_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        authorize(actor, Permission.READ, row["laboratory_id"])
        return self.project(actor, row)

    def list(self, connection, actor, inspection_id, page, page_size, laboratory_id=None):
        parent = (
            connection.execute(
                text("SELECT laboratory_id FROM inspections WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": actor.tenant_id, "id": inspection_id},
            )
            .mappings()
            .first()
        )
        if parent is None:
            raise not_found()
        authorize(actor, Permission.READ, parent["laboratory_id"])
        params = {"tenant": actor.tenant_id, "inspection": inspection_id}
        where = "item.tenant_id=:tenant AND item.inspection_id=:inspection"
        if laboratory_id is not None:
            # OrganizationRepository.get uses a locking current read for commands.
            # Keep this read-only filter in the same snapshot as the parent and page.
            exists = connection.scalar(
                text("SELECT id FROM laboratories WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": actor.tenant_id, "id": laboratory_id},
            )
            if exists is None:
                raise not_found()
            authorize(actor, Permission.READ, laboratory_id)
            where += " AND item.laboratory_id=:lab"
            params["lab"] = laboratory_id
        # Authorization, count, page and parent/item state all use one RR snapshot.
        # An authorized but nonmatching lab filter returns a genuinely empty page.
        total = connection.scalar(text(f"SELECT COUNT(*) FROM {FROM} WHERE {where}"), params)
        rows = (
            connection.execute(
                text(
                    f"SELECT {FIELDS}{INTERNAL_FIELDS} FROM {FROM} WHERE {where} "
                    "ORDER BY item.created_at DESC,item.id DESC LIMIT :limit OFFSET :offset"
                ),
                {**params, "limit": page_size, "offset": (page - 1) * page_size},
            )
            .mappings()
            .all()
        )
        return {
            "items": [self.project(actor, row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
