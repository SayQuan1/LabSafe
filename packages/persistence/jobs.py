"""Image-job read models and atomic generation replay, using domain-first locks."""

from uuid import UUID, uuid4

from sqlalchemy import text

from packages.domain.security import Permission, authorize, not_found
from packages.persistence import job_execution
from packages.persistence.dispatch import db_now, decoded, validate_dispatch
from packages.persistence.foundations import BASE, project
from packages.persistence.security import append_audit
from packages.persistence.users import timestamp
from packages.shared.json_hash import canonical_json

FIELDS = (
    BASE + ",task_type,resource_id,state,attempt,replay_generation,available_at,last_error_code"
)
COLUMNS = ",".join("job." + name for name in FIELDS.split(","))
FROM = (
    "task_runs job JOIN asset_images image ON image.tenant_id=job.tenant_id "
    "AND image.id=job.resource_id"
)


def job_projection(row):
    result = project({name: row[name] for name in FIELDS.split(",")})
    result["available_at"] = timestamp(row["available_at"])
    return result


class JobRepository:
    def get(self, connection, actor, job_id):
        row = (
            connection.execute(
                text(
                    f"SELECT {COLUMNS},image.laboratory_id FROM {FROM} WHERE job.tenant_id=:tenant "
                    "AND job.id=:id AND job.task_type='validate_image'"
                ),
                {"tenant": actor.tenant_id, "id": job_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        authorize(actor, Permission.READ, row["laboratory_id"])
        return job_projection(row)

    def dead_letters(self, connection, actor, page, size, laboratory_id=None):
        authorize(actor, Permission.ADMIN)
        params = {"tenant": actor.tenant_id}
        where = (
            "job.tenant_id=:tenant AND job.task_type='validate_image' "
            "AND job.state IN ('failed','dead_letter')"
        )
        if laboratory_id is not None:
            # Plain snapshot read; do not change RR paging into a locking query.
            lab = connection.scalar(
                text("SELECT id FROM laboratories WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": actor.tenant_id, "id": laboratory_id},
            )
            if lab is None:
                raise not_found()
            params["lab"] = laboratory_id
            where += " AND image.laboratory_id=:lab"
        total = connection.scalar(text(f"SELECT COUNT(*) FROM {FROM} WHERE {where}"), params)
        rows = connection.execute(
            text(
                f"SELECT {COLUMNS} FROM {FROM} WHERE {where} "
                "ORDER BY job.created_at DESC,job.id DESC "
                "LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": size, "offset": (page - 1) * size},
        ).mappings()
        return {
            "items": [job_projection(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": size,
        }

    def replay_source(self, connection, actor, job_id):
        # Same tenant -> owner -> upload -> image -> task order as claim/commit/recover.
        return job_execution._load(connection, actor.tenant_id, job_id)

    def replay(self, connection, actor, task, reason, request_id):
        now = db_now(connection)
        generation, sequence = task["replay_generation"] + 1, task["dispatch_sequence"] + 1
        version = task["version"] + 1
        message = validate_dispatch(
            {
                "schema_version": "1.1",
                "tenant_id": actor.tenant_id,
                "trace_id": UUID(request_id).hex,
                "task_id": task["id"],
                "task_type": "validate_image",
                "resource_id": task["resource_id"],
                "replay_generation": generation,
                "dispatch_sequence": sequence,
                "created_at": timestamp(now),
                "payload": decoded(task["payload"]),
            }
        )
        connection.execute(
            text(
                "UPDATE task_runs SET state='ready',attempt=0,replay_generation=:generation,"
                "fencing_token=fencing_token+1,dispatch_sequence=:sequence,lease_owner=NULL,lease_until=NULL,"
                "heartbeat_at=NULL,started_at=NULL,finished_at=NULL,last_error_code=NULL,available_at=:now,"
                "last_dispatched_at=:now,updated_at=:now,version=:version "
                "WHERE tenant_id=:tenant AND id=:id"
            ),
            {
                "tenant": actor.tenant_id,
                "id": task["id"],
                "generation": generation,
                "sequence": sequence,
                "version": version,
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO outbox_events (id,tenant_id,event_type,aggregate_type,aggregate_id,"
                "aggregate_version,payload,state,available_at,created_at,updated_at) VALUES "
                "(:event,:tenant,'TaskDispatch','task_run',:task,:version,:payload,'pending',:now,:now,:now)"
            ),
            {
                "event": str(uuid4()),
                "tenant": actor.tenant_id,
                "task": task["id"],
                "version": version,
                "payload": canonical_json(message),
                "now": now,
            },
        )
        append_audit(
            connection,
            principal=actor,
            action="job.replay",
            resource_type="job",
            resource_id=task["id"],
            changes={
                "state": "ready",
                "previous_state": task["state"],
                "replay_generation": generation,
                "attempt": 0,
            },
            reason=reason,
            request_id=request_id,
        )
        return self.get(connection, actor, task["id"])
