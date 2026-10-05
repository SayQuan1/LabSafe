"""Application boundary for submitInspectionItem."""

from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import text

from packages.application.identity import retry_deadlocks
from packages.domain.security import Permission, ServiceError, authorize
from packages.domain.workflow import ImageSelection
from packages.persistence.inference_runs import submit
from packages.persistence.security import begin_idempotency, complete_idempotency, utc_now


class InferenceRunApplication:
    def __init__(self, identity):
        self.identity = identity

    def submit(self, token, csrf, key, item_id, body, request_id):
        actor = self.identity._preflight(token, csrf=csrf)
        self.identity.limits.user(actor, write=True)
        return self._submit_once(token, csrf, key, item_id, body, request_id)

    @retry_deadlocks
    def _submit_once(self, token, csrf, key, item_id, body, request_id):
        try:
            item_id = str(UUID(item_id))
            if not isinstance(body, dict) or set(body) != {"expected_version", "images"}:
                raise ServiceError("VALIDATION_ERROR", 422, "Invalid item submit body")
            selections = tuple(
                ImageSelection(
                    image_id=str(UUID(value["image_id"])),
                    role=value["role"],
                    parent_image_id=(
                        str(UUID(value["parent_image_id"])) if value["parent_image_id"] else None
                    ),
                )
                for value in body["images"]
            )
        except (KeyError, TypeError, ValueError):
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid item submit body") from None
        with self.identity.transaction() as connection:
            tenant, actor_id = self.identity.sessions.locate(connection, token)
            owner = str(uuid4())
            claim = begin_idempotency(
                connection,
                tenant_id=tenant,
                actor_id=actor_id,
                method="POST",
                path=f"/api/v1/inspection-items/{item_id}/submit",
                key=key,
                request_hash=self.identity.sessions.request_digest(body),
                expires_at=utc_now() + timedelta(hours=24),
                lease_owner=owner,
            )
            session = self.identity.sessions.authenticate(connection, token)
            self.identity.sessions.require_csrf(session, csrf)
            actor = session.principal
            if claim.state == "completed":
                # Re-check visibility only; do not rerun state/version guards.
                cached = connection.execute(
                    text(
                        "SELECT laboratory_id FROM inspection_items "
                        "WHERE tenant_id=:tenant AND id=:id FOR SHARE"
                    ),
                    {"tenant": actor.tenant_id, "id": item_id},
                ).first()
                if cached is None:
                    raise ServiceError("NOT_FOUND", 404, "Not found")
                authorize(actor, Permission.CAPTURE, cached[0])
                return claim.response_status, claim.response
            data = submit(
                connection, actor, item_id, body["expected_version"], selections, request_id
            )
            response = {"data": data, "request_id": request_id}
            complete_idempotency(
                connection, claim, status=202, response=response, lease_owner=owner
            )
            return 202, response
