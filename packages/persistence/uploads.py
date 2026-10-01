"""Upload ownership and grant reservations; caller owns the transaction and quota mutex."""

from sqlalchemy import text

from packages.domain.security import not_found
from packages.domain.uploads import require_grant_capacity
from packages.domain.workflow import Item, Task
from packages.persistence.security import append_audit


class UploadRepository:
    def get(self, connection, actor, upload_id, *, lock=False):
        row = (
            connection.execute(
                text(
                    "SELECT * FROM uploads WHERE tenant_id=:tenant AND id=:id"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"tenant": actor.tenant_id, "id": upload_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        return dict(row)

    def mark_validating(self, connection, actor, upload_id, request_id):
        changed = connection.execute(
            text(
                "UPDATE uploads SET status='validating',version=version+1,"
                "updated_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant AND id=:id "
                "AND status='granted'"
            ),
            {"tenant": actor.tenant_id, "id": upload_id},
        ).rowcount
        if changed != 1:
            raise RuntimeError("Upload transition lost its locked precondition")
        append_audit(
            connection,
            principal=actor,
            action="upload.complete",
            resource_type="upload",
            resource_id=upload_id,
            changes={"status": "validating"},
            reason="Accept upload for asynchronous image validation",
            request_id=request_id,
        )

    def owner(self, connection, actor, owner_type, owner_id):
        params = {"tenant": actor.tenant_id, "id": owner_id}
        if owner_type == "inspection_item":
            parent_id = connection.scalar(
                text(
                    "SELECT inspection_id FROM inspection_items WHERE tenant_id=:tenant AND id=:id"
                ),
                params,
            )
            if parent_id is None:
                raise not_found()
            parent = (
                connection.execute(
                    text(
                        "SELECT id,laboratory_id,status,inspector_id FROM inspections "
                        "WHERE tenant_id=:tenant AND id=:parent FOR UPDATE"
                    ),
                    {**params, "parent": parent_id},
                )
                .mappings()
                .first()
            )
            row = (
                connection.execute(
                    text(
                        "SELECT id,tenant_id,laboratory_id,version,status,location_id,"
                        "current_run_id,current_evaluation_id,inspection_id FROM inspection_items "
                        "WHERE tenant_id=:tenant AND id=:id FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if (
                parent is None
                or row is None
                or row["inspection_id"] != parent["id"]
                or row["laboratory_id"] != parent["laboratory_id"]
            ):
                raise not_found()
            return Item(
                **{
                    k: row[k]
                    for k in Item.__dataclass_fields__
                    if k not in {"inspection_status", "creator_id"}
                },
                inspection_status=parent["status"],
                creator_id=parent["inspector_id"],
            )
        if owner_type == "remediation_task":
            row = (
                connection.execute(
                    text(
                        "SELECT id,tenant_id,laboratory_id,version,status,"
                        "finding_id,assignee_id,latest_evidence_id FROM remediation_tasks "
                        "WHERE tenant_id=:tenant AND id=:id FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if row is None:
                raise not_found()
            return Task(**dict(row))
        raise ValueError("Unknown upload owner type")

    def reserve(
        self,
        connection,
        actor,
        owner,
        body,
        *,
        upload_id,
        key,
        expires_at,
        captured_at,
        now,
        request_id,
    ):
        # Current read after waiting for the user mutex, not the locator's old RR snapshot.
        expirations = (
            connection.execute(
                text(
                    "SELECT expires_at FROM uploads "
                    "WHERE tenant_id=:tenant AND requested_by=:user AND status='granted' "
                    "AND expires_at>:now FOR SHARE"
                ),
                {"tenant": actor.tenant_id, "user": actor.user_id, "now": now},
            )
            .scalars()
            .all()
        )
        require_grant_capacity(expirations, now)
        connection.execute(
            text(
                "INSERT INTO uploads (id,tenant_id,laboratory_id,inspection_item_id,"
                "remediation_task_id,requested_by,object_key,expected_sha256,mime_type,size_bytes,"
                "captured_at,expires_at) VALUES "
                "(:id,:tenant,:lab,:item,:task,:user,:key,:sha,:mime,"
                ":size,:captured,:expires)"
            ),
            {
                "id": upload_id,
                "tenant": actor.tenant_id,
                "lab": owner.laboratory_id,
                "item": owner.id if body["owner_type"] == "inspection_item" else None,
                "task": owner.id if body["owner_type"] == "remediation_task" else None,
                "user": actor.user_id,
                "key": key,
                "sha": body["sha256"],
                "mime": body["mime_type"],
                "size": body["size_bytes"],
                "captured": captured_at,
                "expires": expires_at,
            },
        )
        append_audit(
            connection,
            principal=actor,
            action="upload.create",
            resource_type="upload",
            resource_id=upload_id,
            changes={"status": "granted"},
            reason="Create upload grant",
            request_id=request_id,
        )
