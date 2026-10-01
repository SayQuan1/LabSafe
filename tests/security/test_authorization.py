import pytest

from packages.domain.security import (
    Permission,
    Principal,
    ServiceError,
    authorize,
    permission_projection,
    require_version,
)
from packages.persistence.security import SESSION_ABSOLUTE, SESSION_IDLE, utc_now


def principal(*roles):
    return Principal("u1", "t1", "user", tuple(roles))


def test_laboratory_scope_does_not_cross_laboratories():
    p = principal(("inspector", "laboratory", "lab-a"))
    authorize(p, Permission.READ, "lab-a")
    with pytest.raises(ServiceError) as error:
        authorize(p, Permission.READ, "lab-b")
    assert error.value.code == "NOT_FOUND"


def test_remediator_must_be_current_assignee():
    p = principal(("remediator", "laboratory", "lab-a"))
    with pytest.raises(ServiceError):
        authorize(p, Permission.ASSIGNEE, "lab-a", assignee_id="u2")
    authorize(p, Permission.ASSIGNEE, "lab-a", assignee_id="u1")


def test_rule_expert_cannot_approve_own_submission():
    p = principal(("rule_expert", "tenant", None))
    with pytest.raises(ServiceError):
        authorize(p, Permission.RULE_APPROVE, submitted_by="u1")
    authorize(p, Permission.RULE_APPROVE, submitted_by="u2")


def test_recheck_rejects_assignee_or_submitter():
    p = principal(("inspector", "laboratory", "lab-a"))
    with pytest.raises(ServiceError):
        authorize(p, Permission.RECHECK, "lab-a", assignee_id="u1")
    with pytest.raises(ServiceError):
        authorize(p, Permission.RECHECK, "lab-a", submitted_by="u1")


def test_unknown_role_and_scope_are_denied():
    with pytest.raises(ServiceError):
        authorize(principal(("unknown", "tenant", None)), Permission.READ)
    with pytest.raises(ServiceError):
        authorize(principal(("inspector", "tenant", None)), Permission.READ)


def test_version_guard_is_strict():
    require_version(2, 2)
    with pytest.raises(ServiceError) as error:
        require_version(2, 1)
    assert error.value.code == "VERSION_CONFLICT"
    with pytest.raises(ServiceError):
        require_version(1, True)


def test_time_contract_is_millisecond_utc_and_expiry_windows_are_fixed():
    now = utc_now()
    assert now.microsecond % 1000 == 0
    assert SESSION_ABSOLUTE.total_seconds() == 8 * 3600
    assert SESSION_IDLE.total_seconds() == 30 * 60


def test_permission_projection_deduplicates_and_rejects_over_capacity():
    roles = tuple(("viewer", "laboratory", f"lab-{i:03}") for i in range(200))
    projected = permission_projection(principal(*roles, roles[0]))
    assert len(projected) == 200
    assert projected == sorted(projected, key=lambda p: (p["action"], p["laboratory_id"]))
    with pytest.raises(ServiceError) as failure:
        permission_projection(principal(*roles, ("viewer", "laboratory", "lab-200")))
    assert failure.value.code == "STATE_CONFLICT"
