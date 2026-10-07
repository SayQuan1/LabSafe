"""Report snapshot acceptance; all writes commit with the idempotent response."""

from datetime import timedelta
from uuid import uuid4

from packages.application.identity import retry_deadlocks
from packages.domain.report_download import REPORT_DOWNLOAD_SECONDS, download_reference
from packages.domain.security import Permission, ServiceError
from packages.persistence import reports
from packages.persistence.dispatch import decoded
from packages.persistence.security import begin_idempotency, complete_idempotency, utc_now
from packages.persistence.transaction import lock_tenant
from packages.persistence.users import timestamp


class ReportApplication:
    def __init__(self, identity, storage=None):
        self.identity, self.storage = identity, storage

    def create(self, token, csrf, key, body, request_id):
        actor = self.identity._preflight(token, csrf=csrf)
        self.identity.limits.user(actor, write=True)
        return self._create_once(token, csrf, key, body, request_id)

    @retry_deadlocks
    def _create_once(self, token, csrf, key, body, request_id):
        with self.identity.transaction() as connection:
            tenant, actor_id = self.identity.sessions.locate(connection, token)
            lock_tenant(connection, tenant)
            owner = str(uuid4())
            claim = begin_idempotency(
                connection,
                tenant_id=tenant,
                actor_id=actor_id,
                method="POST",
                path="/api/v1/reports/exports",
                key=key,
                request_hash=self.identity.sessions.request_digest(body),
                expires_at=utc_now() + timedelta(hours=24),
                lease_owner=owner,
            )
            session = self.identity.sessions.authenticate(connection, token)
            self.identity.sessions.require_csrf(session, csrf)
            if claim.state == "completed":
                reports.get(
                    connection,
                    session.principal,
                    claim.response["data"]["id"],
                    permission=Permission.EXPORT,
                    current=True,
                )
                return claim.response_status, claim.response
            data = reports.create(connection, session.principal, body, request_id)
            response = {"data": data, "request_id": request_id}
            complete_idempotency(
                connection, claim, status=202, response=response, lease_owner=owner
            )
            return 202, response

    def read(self, token, request_id, export_id):
        actor = self.identity._preflight(token)
        self.identity.limits.user(actor, write=False)
        return self._read_once(token, request_id, export_id)

    @retry_deadlocks
    def _read_once(self, token, request_id, export_id):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            return {"data": reports.get(connection, actor, export_id), "request_id": request_id}

    def download(self, token, export_id, request_id):
        if self.storage is None:
            raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Report downloads not configured")
        actor = self.identity._preflight(token)
        self.identity.limits.download_grant(actor)
        return self._download_once(token, export_id, request_id)

    @retry_deadlocks
    def _download_once(self, token, export_id, request_id):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            row = reports.download_row(connection, actor, export_id)
            if row is not None:
                try:
                    labs = decoded(row["filters"])["laboratory_ids"]
                except (TypeError, ValueError, KeyError):
                    labs = None
                if isinstance(labs, list) and all(isinstance(lab, str) for lab in labs):
                    # Full snapshot scope is required even for a creator/admin.
                    reports.authorize_labs(connection, actor, labs, Permission.EXPORT)
            reference = download_reference(actor, row, utc_now())
            grant = self.storage.presign_report_get(reference)
            remaining = (grant.expires_at - utc_now()).total_seconds()
            if not 0 < remaining <= REPORT_DOWNLOAD_SECONDS:
                raise ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Invalid download lifetime")
            return {
                "data": {"url": grant.url, "expires_at": timestamp(grant.expires_at)},
                "request_id": request_id,
            }
