"""Image metadata projection; no object URLs, keys, versions or decoding side effects."""

from sqlalchemy import text

from packages.domain.security import Permission, authorize, not_found
from packages.domain.uploads import owner_reference
from packages.persistence.foundations import project
from packages.persistence.users import timestamp

PUBLIC_FIELDS = (
    "id",
    "created_at",
    "updated_at",
    "version",
    "laboratory_id",
    "status",
    "original_sha256",
    "analysis_sha256",
    "width",
    "height",
    "mime_type",
    "captured_at",
)


class ImageRepository:
    def download_row(self, connection, actor, image_id):
        # Locking current read: a concurrent deletion/input update cannot race signing.
        # User/role/session locks from authenticate are already held by the caller.
        return (
            connection.execute(
                text("SELECT * FROM asset_images WHERE tenant_id=:tenant AND id=:id FOR SHARE"),
                {"tenant": actor.tenant_id, "id": image_id},
            )
            .mappings()
            .first()
        )

    @staticmethod
    def project(row):
        data = project({name: row[name] for name in PUBLIC_FIELDS})
        data["captured_at"] = timestamp(row["captured_at"])
        data["owner_type"], data["owner_id"] = owner_reference(row)
        return data

    def get(self, connection, actor, image_id):
        row = (
            connection.execute(
                text("SELECT * FROM asset_images WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": actor.tenant_id, "id": image_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        # Both owner FKs also bind laboratory_id. Read does not require capture permission.
        authorize(actor, Permission.READ, row["laboratory_id"])
        return self.project(row)

    def for_upload(self, connection, actor, upload_id):
        return (
            connection.execute(
                text(
                    "SELECT * FROM asset_images WHERE tenant_id=:tenant "
                    "AND upload_id=:upload FOR UPDATE"
                ),
                {"tenant": actor.tenant_id, "upload": upload_id},
            )
            .mappings()
            .first()
        )

    def create_validating(self, connection, upload, pinned, image_id):
        connection.execute(
            text(
                "INSERT INTO asset_images (id,tenant_id,laboratory_id,inspection_item_id,"
                "remediation_task_id,upload_id,original_key,original_object_version,"
                "original_sha256,mime_type,captured_at) VALUES "
                "(:id,:tenant,:lab,:item,:task,:upload,:key,:version,:sha,:mime,:captured)"
            ),
            {
                "id": image_id,
                "tenant": upload["tenant_id"],
                "lab": upload["laboratory_id"],
                "item": upload["inspection_item_id"],
                "task": upload["remediation_task_id"],
                "upload": upload["id"],
                "key": pinned.key,
                "version": pinned.version_id,
                # A declared digest until worker verification. Never mark ready here.
                "sha": upload["expected_sha256"],
                "mime": upload["mime_type"],
                "captured": upload["captured_at"],
            },
        )
