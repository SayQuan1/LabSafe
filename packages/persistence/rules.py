"""Tenant-scoped rule configuration repositories.

Rule versions and bundles are immutable snapshots once published/created.  The
repository owns lifecycle transitions so API handlers never update status or
tenant columns directly.
"""

import json
from uuid import uuid4

from sqlalchemy import text

from packages.domain.security import Permission, ServiceError, authorize, not_found, require_version
from packages.persistence.foundations import page_query, project
from packages.persistence.security import append_audit, safe_text
from packages.rules.evaluator import RuleDefinitionError, validate_bundle
from packages.shared.json_hash import canonical_json, sha256_json


def _rules(definition):
    try:
        value = json.loads(definition) if isinstance(definition, str) else definition
    except (TypeError, ValueError):
        return []
    if isinstance(value, dict) and isinstance(value.get("rules"), list):
        return value["rules"]
    if isinstance(value, dict):
        return [value]
    return value if isinstance(value, list) else []


def _manage(actor):
    try:
        authorize(actor, Permission.ADMIN)
        return True
    except ServiceError:
        return False


def _rule_expert(actor):
    try:
        authorize(actor, Permission.RULE_APPROVE)
        return True
    except ServiceError:
        return False


def _read_scope(actor, laboratory_id=None):
    try:
        if laboratory_id is not None:
            authorize(actor, Permission.READ, laboratory_id)
        else:
            authorize(actor, Permission.READ, published=True)
    except ServiceError:
        # Rule experts are tenant-scoped reviewers.  The generic READ
        # projection intentionally excludes that role from ordinary lab data,
        # but rule configuration is their explicit responsibility.
        authorize(actor, Permission.RULE_APPROVE)


class RuleRepository:
    def create_set(self, connection, actor, body, request_id):
        authorize(actor, Permission.ADMIN)
        name = safe_text(body.get("name"), 200)
        description = safe_text(body.get("description"), 2000)
        resource_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO rule_sets (id,tenant_id,name,description) "
                "VALUES (:id,:tenant,:name,:description)"
            ),
            {
                "id": resource_id,
                "tenant": actor.tenant_id,
                "name": name,
                "description": description,
            },
        )
        append_audit(
            connection,
            principal=actor,
            action="rule_set.create",
            resource_type="rule_set",
            resource_id=resource_id,
            changes={"name": name},
            reason="Create rule set",
            request_id=request_id,
        )
        return self.get_set(connection, actor, resource_id, share=True)

    def get_set(self, connection, actor, resource_id, *, share=False):
        row = (
            connection.execute(
                text(
                    "SELECT id,created_at,updated_at,version,name,description "
                    "FROM rule_sets WHERE tenant_id=:tenant AND id=:id"
                    + (" FOR SHARE" if share else "")
                ),
                {"tenant": actor.tenant_id, "id": resource_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        _read_scope(actor)
        return project(row)

    def list_sets(self, connection, actor, page, page_size, laboratory_id=None):
        _read_scope(actor, laboratory_id)
        return page_query(
            connection,
            "rule_sets",
            "id,created_at,updated_at,version,name,description",
            "tenant_id=:tenant",
            {"tenant": actor.tenant_id},
            page,
            page_size,
        )

    def _version_row(self, connection, actor, resource_id, *, lock=False):
        row = (
            connection.execute(
                text(
                    "SELECT v.*,s.name AS rule_set_name FROM rule_versions v "
                    "JOIN rule_sets s ON s.tenant_id=v.tenant_id AND s.id=v.rule_set_id "
                    "WHERE v.tenant_id=:tenant AND v.id=:id" + (" FOR UPDATE" if lock else "")
                ),
                {"tenant": actor.tenant_id, "id": resource_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        if row["status"] not in {"published", "retired"} and not (
            _manage(actor) or _rule_expert(actor)
        ):
            raise not_found()
        return row

    @staticmethod
    def _version_projection(row):
        value = project(row)
        value["rules"] = _rules(row["definition"])
        value.pop("definition", None)
        value.pop("tenant_id", None)
        value.pop("rule_set_name", None)
        value.pop("submitted_by", None)
        value.pop("approved_at", None)
        return value

    def get_version(self, connection, actor, resource_id, *, share=False):
        row = self._version_row(connection, actor, resource_id, lock=False)
        if share:
            # The current read is already visibility-filtered; the optional
            # argument is retained for idempotency revalidation symmetry.
            pass
        return self._version_projection(row)

    def list_versions(self, connection, actor, page, page_size, laboratory_id=None):
        _read_scope(actor, laboratory_id)
        params = {"tenant": actor.tenant_id}
        where = "v.tenant_id=:tenant"
        if not (_manage(actor) or _rule_expert(actor)):
            where += " AND v.status IN ('published','retired')"
        return page_query(
            connection,
            "rule_versions v",
            "v.id,v.created_at,v.updated_at,v.version,v.rule_set_id,v.revision,v.checksum,"
            "v.status,v.definition,v.tenant_id",
            where,
            params,
            page,
            page_size,
            project_row=self._version_projection,
        )

    def import_version(self, connection, actor, body, request_id):
        authorize(actor, Permission.ADMIN)
        bundle = body.get("bundle")
        reason = safe_text(body.get("reason"), 2000)
        try:
            validate_bundle(bundle)
        except (RuleDefinitionError, TypeError, KeyError):
            raise ServiceError("RULESET_INVALID", 422, "Rule bundle is invalid") from None
        laboratory_ids = {
            rule["scope_id"] for rule in bundle["rules"] if rule["scope"] == "laboratory"
        }
        if any(
            rule["scope"] == "tenant" and rule["scope_id"] != actor.tenant_id
            for rule in bundle["rules"]
        ):
            raise ServiceError(
                "VALIDATION_ERROR", 422, "Tenant-scoped rule has an invalid scope_id"
            )
        if laboratory_ids:
            placeholders = ",".join(f":lab{i}" for i in range(len(laboratory_ids)))
            count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM laboratories WHERE tenant_id=:tenant "
                    f"AND id IN ({placeholders})"
                ),
                {
                    "tenant": actor.tenant_id,
                    **{f"lab{i}": value for i, value in enumerate(sorted(laboratory_ids))},
                },
            )
            if count != len(laboratory_ids):
                raise ServiceError(
                    "VALIDATION_ERROR", 422, "Laboratory-scoped rule has an invalid scope_id"
                )
        rule_set_id = bundle["rule_set_id"]
        set_row = (
            connection.execute(
                text("SELECT id FROM rule_sets WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                {"tenant": actor.tenant_id, "id": rule_set_id},
            )
            .mappings()
            .first()
        )
        if set_row is None:
            raise not_found()
        revision = connection.scalar(
            text(
                "SELECT COALESCE(MAX(revision),0)+1 FROM rule_versions "
                "WHERE tenant_id=:tenant AND rule_set_id=:set"
            ),
            {"tenant": actor.tenant_id, "set": rule_set_id},
        )
        resource_id = str(uuid4())
        checksum = sha256_json(bundle)
        connection.execute(
            text(
                "INSERT INTO rule_versions "
                "(id,tenant_id,rule_set_id,revision,checksum,definition,status,submitted_by) "
                "VALUES (:id,:tenant,:set,:revision,:checksum,:definition,'draft',:submitted)"
            ),
            {
                "id": resource_id,
                "tenant": actor.tenant_id,
                "set": rule_set_id,
                "revision": revision,
                "checksum": checksum,
                "definition": canonical_json(bundle),
                "submitted": actor.user_id,
            },
        )
        append_audit(
            connection,
            principal=actor,
            action="rule_version.import",
            resource_type="rule_version",
            resource_id=resource_id,
            changes={"rule_set_id": rule_set_id, "revision": revision, "checksum": checksum},
            reason=reason,
            request_id=request_id,
        )
        return self.get_version(connection, actor, resource_id, share=True)

    def transition(self, connection, actor, resource_id, operation, body, request_id):
        row = self._version_row(connection, actor, resource_id, lock=True)
        expected = body.get("expected_version")
        reason = safe_text(body.get("reason"), 2000)
        require_version(row["version"], expected)
        if operation == "submit":
            if row["status"] != "draft":
                raise ServiceError("STATE_CONFLICT", 409, "Rule version is not a draft")
            values = {"status": "submitted", "approved": None, "approved_at": None}
        elif operation == "approve":
            authorize(actor, Permission.RULE_APPROVE, submitted_by=row["submitted_by"])
            if row["status"] != "submitted":
                raise ServiceError("STATE_CONFLICT", 409, "Rule version is not awaiting approval")
            values = {
                "status": "approved",
                "approved": actor.user_id,
                "approved_at": "UTC_TIMESTAMP(3)",
            }
        elif operation == "publish":
            authorize(actor, Permission.ADMIN)
            if row["status"] != "approved" or not row["approved_by"]:
                raise ServiceError("STATE_CONFLICT", 409, "Rule version is not approved")
            values = {"status": "published", "approved": row["approved_by"]}
        elif operation == "retire":
            authorize(actor, Permission.ADMIN)
            if row["status"] != "published":
                raise ServiceError("STATE_CONFLICT", 409, "Only published rules can be retired")
            values = {"status": "retired", "approved": row["approved_by"]}
        else:
            raise ValueError("Unknown rule version transition")
        params = {
            "tenant": actor.tenant_id,
            "id": resource_id,
            "status": values["status"],
            "approved": values["approved"],
        }
        approved_at = ",approved_at=UTC_TIMESTAMP(3)" if operation == "approve" else ""
        connection.execute(
            text(
                "UPDATE rule_versions SET status=:status,approved_by=:approved"
                + approved_at
                + ",version=version+1,updated_at=UTC_TIMESTAMP(3) "
                "WHERE tenant_id=:tenant AND id=:id AND version=:expected"
            ),
            {**params, "expected": expected},
        )
        append_audit(
            connection,
            principal=actor,
            action=f"rule_version.{operation}",
            resource_type="rule_version",
            resource_id=resource_id,
            changes={"status": values["status"], "version": row["version"] + 1},
            reason=reason,
            request_id=request_id,
        )
        return self.get_version(connection, actor, resource_id, share=True)

    def create_bundle(self, connection, actor, body, request_id):
        authorize(actor, Permission.ADMIN)
        raw_ids = body.get("rule_version_ids")
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 100:
            raise ServiceError("VALIDATION_ERROR", 422, "Invalid rule_version_ids")
        ids = [str(value) for value in raw_ids]
        if len(ids) != len(set(ids)):
            raise ServiceError("VALIDATION_ERROR", 422, "Duplicate rule_version_ids")
        rows = (
            connection.execute(
                text(
                    "SELECT id,status,rule_set_id FROM rule_versions WHERE tenant_id=:tenant "
                    "AND id IN (" + ",".join(f":id{i}" for i in range(len(ids))) + ") "
                    "FOR UPDATE"
                ),
                {"tenant": actor.tenant_id, **{f"id{i}": value for i, value in enumerate(ids)}},
            )
            .mappings()
            .all()
        )
        if len(rows) != len(ids) or any(row["status"] != "published" for row in rows):
            raise ServiceError("STATE_CONFLICT", 409, "Only published rule versions can be bundled")
        ordered = sorted(ids)
        checksum = sha256_json(ordered)
        existing = (
            connection.execute(
                text("SELECT * FROM rule_bundles WHERE tenant_id=:tenant AND checksum=:checksum"),
                {"tenant": actor.tenant_id, "checksum": checksum},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            return self._bundle_projection(connection, actor.tenant_id, existing)
        resource_id = str(uuid4())
        connection.execute(
            text(
                "INSERT INTO rule_bundles (id,tenant_id,checksum,evaluator_version) "
                "VALUES (:id,:tenant,:checksum,'rules-dnf-v1')"
            ),
            {"id": resource_id, "tenant": actor.tenant_id, "checksum": checksum},
        )
        connection.execute(
            text(
                "INSERT INTO rule_bundle_members "
                "(id,tenant_id,bundle_id,rule_version_id) VALUES (:id,:tenant,:bundle,:version)"
            ),
            [
                {
                    "id": str(uuid4()),
                    "tenant": actor.tenant_id,
                    "bundle": resource_id,
                    "version": value,
                }
                for value in ordered
            ],
        )
        append_audit(
            connection,
            principal=actor,
            action="rule_bundle.create",
            resource_type="rule_bundle",
            resource_id=resource_id,
            changes={"checksum": checksum, "rule_version_ids": ordered},
            reason="Create immutable rule bundle",
            request_id=request_id,
        )
        row = (
            connection.execute(
                text("SELECT * FROM rule_bundles WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": actor.tenant_id, "id": resource_id},
            )
            .mappings()
            .first()
        )
        return self._bundle_projection(connection, actor.tenant_id, row)

    @staticmethod
    def _bundle_projection(connection, tenant, row):
        value = project(row)
        value["rule_version_ids"] = list(
            connection.execute(
                text(
                    "SELECT rule_version_id FROM rule_bundle_members "
                    "WHERE tenant_id=:tenant AND bundle_id=:bundle ORDER BY rule_version_id"
                ),
                {"tenant": tenant, "bundle": row["id"]},
            ).scalars()
        )
        value.pop("tenant_id", None)
        return value

    def list_bundles(self, connection, actor, page, page_size, laboratory_id=None):
        _read_scope(actor, laboratory_id)
        return page_query(
            connection,
            "rule_bundles",
            "id,created_at,updated_at,version,tenant_id,checksum,evaluator_version",
            "tenant_id=:tenant",
            {"tenant": actor.tenant_id},
            page,
            page_size,
            project_row=lambda row: self._bundle_projection(connection, actor.tenant_id, row),
        )
