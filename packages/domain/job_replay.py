"""Manual image/report task replay guards; no queue or object calls."""

from packages.domain.security import Permission, ServiceError, authorize, not_found, require_version


def replay_body(body):
    if (
        not isinstance(body, dict)
        or set(body) != {"expected_version", "reason"}
        or not isinstance(body["reason"], str)
        or not 1 <= len(body["reason"]) <= 2000
        or not body["reason"].strip()
    ):
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid replay command")
    require_version(body["expected_version"], body["expected_version"])


def require_replay(actor, task, current, expected_version):
    authorize(actor, Permission.ADMIN)
    if (
        task is None
        or task["tenant_id"] != actor.tenant_id
        or task["task_type"] not in {"validate_image", "report_export"}
    ):
        raise not_found()
    require_version(task["version"], expected_version)
    if task["state"] not in {"dead_letter", "failed"} or current is None:
        raise ServiceError("STATE_CONFLICT", 409, "Task source is not replayable")
    if any(
        task[name] >= 2147483647 for name in ("replay_generation", "dispatch_sequence", "version")
    ):
        raise ServiceError("STATE_CONFLICT", 409, "Task replay counter exhausted")
