"""Upload grants and atomic validation acceptance; never an image-ready shortcut."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from packages.application.identity import retry_deadlocks
from packages.domain.security import ServiceError, not_found
from packages.domain.uploads import (
    MAX_BYTES,
    authorize_upload,
    owner_reference,
    require_completion,
    require_staging_match,
    staging_key,
    validate_complete_request,
    validate_upload_request,
)
from packages.persistence.image_jobs import enqueue_image_validation
from packages.persistence.images import ImageRepository
from packages.persistence.security import begin_idempotency, complete_idempotency, utc_now
from packages.persistence.transaction import lock_tenant
from packages.persistence.uploads import UploadRepository
from packages.persistence.users import timestamp


class _NeedsStagingInspection(Exception):
    """Roll back the entire preparation transaction before any object I/O."""

    def __init__(self, upload):
        self.upload = upload


class UploadGrantApplication:
    def __init__(self, identity, storage):
        self.identity, self.storage = identity, storage
        self.uploads = UploadRepository()
        self.images = ImageRepository()

    def create(self, token, csrf, key, body, request_id):
        captured = validate_upload_request(body)
        principal = self.identity._preflight(token, csrf=csrf)
        self.identity.limits.user(principal, write=True)
        self.identity.limits.upload_grant(principal)
        return self._create_once(token, csrf, key, body, captured, request_id)

    @retry_deadlocks
    def _create_once(self, token, csrf, key, body, captured, request_id):
        with self.identity.transaction() as connection:
            tenant, user = self.identity.sessions.locate(connection, token)
            lock_tenant(connection, tenant)
            # Acquire X before authenticate/claim would hold S on the same user.
            # This is a per-user grant mutex, not a tenant-wide business write lock.
            status = connection.scalar(
                text("SELECT status FROM users WHERE tenant_id=:tenant AND id=:user FOR UPDATE"),
                {"tenant": tenant, "user": user},
            )
            if status != "active":
                raise ServiceError("UNAUTHENTICATED", 401, "Authentication required")
            lease = str(uuid4())
            claim = begin_idempotency(
                connection,
                tenant_id=tenant,
                actor_id=user,
                method="POST",
                path="/api/v1/uploads",
                key=key,
                request_hash=self.identity.sessions.request_digest(body),
                expires_at=utc_now() + timedelta(hours=24),
                lease_owner=lease,
            )
            session = self.identity.sessions.authenticate(connection, token)
            self.identity.sessions.require_csrf(session, csrf)
            actor = session.principal
            owner = self.uploads.owner(connection, actor, body["owner_type"], body["owner_id"])
            authorize_upload(actor, owner, check_state=claim.state != "completed")
            if claim.state == "completed":
                return (
                    claim.response_status,
                    claim.response,
                )  # Original URL/expiry, never extend TTL.
            upload_id = str(uuid4())
            object_key = staging_key(tenant, upload_id)
            # Explicit-credential SDK presigning is LOCAL cryptography, not an object call.
            # HEAD/GET/PUT and credential refresh/network must never be added here.
            grant = self.storage.presign_put(tenant, upload_id, body["mime_type"])
            now = utc_now()
            if not now < grant.expires_at <= now + timedelta(seconds=600):
                raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Invalid upload grant lifetime")
            self.uploads.reserve(
                connection,
                actor,
                owner,
                body,
                upload_id=upload_id,
                key=object_key,
                expires_at=grant.expires_at,
                captured_at=captured,
                now=now,
                request_id=request_id,
            )
            response = {
                "data": {
                    "upload_id": upload_id,
                    "object_key": object_key,
                    "put_url": grant.url,
                    "required_content_type": body["mime_type"],
                    "expires_at": timestamp(grant.expires_at),
                    "max_bytes": MAX_BYTES,
                },
                "request_id": request_id,
            }
            complete_idempotency(
                connection, claim, status=201, response=response, lease_owner=lease
            )
            return 201, response

    def complete(self, token, csrf, key, body, request_id):
        validate_complete_request(body)
        principal = self.identity._preflight(token, csrf=csrf)
        self.identity.limits.user(principal, write=True)
        try:
            return self._complete_once(token, csrf, key, body, request_id)
        except _NeedsStagingInspection as preparation:
            upload = preparation.upload
        # No open transaction, persisted pending claim, mutex, or owner lock at this point.
        pinned = self.storage.inspect_staging(upload["tenant_id"], upload["id"])
        return self._complete_once(token, csrf, key, body, request_id, observation=(upload, pinned))

    @retry_deadlocks
    def _complete_once(self, token, csrf, key, body, request_id, *, observation=None):
        with self.identity.transaction() as connection:
            tenant, user = self.identity.sessions.locate(connection, token)
            lock_tenant(connection, tenant)
            # Same actor mutex order as grant creation, before FK/auth shared reads.
            status = connection.scalar(
                text("SELECT status FROM users WHERE tenant_id=:tenant AND id=:user FOR UPDATE"),
                {"tenant": tenant, "user": user},
            )
            if status != "active":
                raise ServiceError("UNAUTHENTICATED", 401, "Authentication required")
            lease = str(uuid4())
            claim = begin_idempotency(
                connection,
                tenant_id=tenant,
                actor_id=user,
                method="POST",
                path="/api/v1/uploads/complete",
                key=key,
                request_hash=self.identity.sessions.request_digest(body),
                expires_at=utc_now() + timedelta(hours=24),
                lease_owner=lease,
            )
            session = self.identity.sessions.authenticate(connection, token)
            self.identity.sessions.require_csrf(session, csrf)
            actor = session.principal
            located = self.uploads.get(connection, actor, body["upload_id"])
            owner = self.uploads.owner(connection, actor, *owner_reference(located))
            upload = self.uploads.get(connection, actor, body["upload_id"], lock=True)
            if (
                owner_reference(upload) != owner_reference(located)
                or owner.laboratory_id != upload["laboratory_id"]
            ):
                raise ServiceError("STATE_CONFLICT", 409, "Upload owner changed")
            authorize_upload(actor, owner, check_state=False)
            image = self.images.for_upload(connection, actor, upload["id"])
            require_completion(
                upload, body["sha256"], utc_now(), already_completed=image is not None
            )
            if image is not None:
                if image["status"] == "deleted":
                    raise not_found()
                if (
                    owner_reference(image) != owner_reference(upload)
                    or image["laboratory_id"] != upload["laboratory_id"]
                    or image["original_sha256"] != upload["expected_sha256"]
                ):
                    raise RuntimeError("Persisted image does not match its upload")
                task = connection.scalar(
                    text(
                        "SELECT id FROM task_runs WHERE tenant_id=:tenant "
                        "AND task_type='validate_image' AND resource_id=:image "
                        "AND logical_key=:logical FOR SHARE"
                    ),
                    {
                        "tenant": tenant,
                        "image": image["id"],
                        "logical": f"validate_image:{image['id']}",
                    },
                )
                if task is None:
                    raise RuntimeError("Persisted image has no validation task")
                if claim.state == "completed":
                    return claim.response_status, claim.response
            else:
                if claim.state == "completed":
                    raise RuntimeError("Completed response has no image")
                authorize_upload(actor, owner)
                if observation is None:
                    # Raising exits/rolls back the transaction, including its idempotency claim.
                    raise _NeedsStagingInspection(upload)
                observed, pinned = observation
                if upload != observed:
                    raise ServiceError("STATE_CONFLICT", 409, "Upload changed during inspection")
                require_staging_match(upload, pinned)
                image_id = str(uuid4())
                self.images.create_validating(connection, upload, pinned, image_id)
                enqueue_image_validation(
                    connection,
                    tenant_id=tenant,
                    upload_id=upload["id"],
                    image_id=image_id,
                    request_id=request_id,
                )
                self.uploads.mark_validating(connection, actor, upload["id"], request_id)
                image = self.images.for_upload(connection, actor, upload["id"])
            response = {"data": self.images.project(image), "request_id": request_id}
            complete_idempotency(
                connection, claim, status=202, response=response, lease_owner=lease
            )
            return 202, response

    def get_image(self, token, image_id, request_id):
        principal = self.identity._preflight(token)
        self.identity.limits.user(principal, write=False)
        return self._get_image_once(token, image_id, request_id)

    @retry_deadlocks
    def _get_image_once(self, token, image_id, request_id):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            return {
                "data": self.images.get(connection, actor, image_id),
                "request_id": request_id,
            }
