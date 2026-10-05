"""Local signing under current authorization locks; original grant and audit commit together."""

from packages.application.identity import retry_deadlocks
from packages.domain.image_download import DOWNLOAD_SECONDS, download_reference
from packages.domain.security import ServiceError
from packages.persistence.images import ImageRepository
from packages.persistence.security import append_audit, utc_now
from packages.persistence.users import timestamp


class ImageDownloadApplication:
    def __init__(self, identity, storage):
        self.identity, self.storage = identity, storage
        self.images = ImageRepository()

    def download(self, token, image_id, variant, request_id):
        if variant not in {"analysis", "original"}:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid download variant")
        actor = self.identity._preflight(token)
        # Both variants issue bearer capabilities; Redis failure must not use read fallback.
        self.identity.limits.download_grant(actor)
        return self._download_once(token, image_id, variant, request_id)

    @retry_deadlocks
    def _download_once(self, token, image_id, variant, request_id):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            reference = download_reference(
                actor, self.images.download_row(connection, actor, image_id), variant
            )
            # Explicit fixed credentials: local cryptography only. Never HEAD/GET here.
            grant = self.storage.presign_image_get(reference)
            remaining = (grant.expires_at - utc_now()).total_seconds()
            if not 0 < remaining <= DOWNLOAD_SECONDS:
                raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Invalid download lifetime")
            if variant == "original":
                append_audit(
                    connection,
                    principal=actor,
                    action="image.download_original",
                    resource_type="image",
                    resource_id=image_id,
                    changes={"variant": "original"},
                    reason="Authorized original image download",
                    request_id=request_id,
                )
            return {
                "data": {"url": grant.url, "expires_at": timestamp(grant.expires_at)},
                "request_id": request_id,
            }
