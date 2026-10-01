"""Pure authorization and optimistic guards; no HTTP, SQL, or client tenant IDs."""

from dataclasses import dataclass
from enum import StrEnum


class ServiceError(ValueError):
    def __init__(self, code: str, status: int, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after


def not_found() -> ServiceError:
    return ServiceError("NOT_FOUND", 404, "Not found")


class Permission(StrEnum):
    READ = "read"
    ADMIN = "admin"
    CAPTURE = "capture"
    REVIEW = "review"
    DISPATCH = "dispatch"
    ASSIGNEE = "assignee"
    RECHECK = "recheck"
    RULE_APPROVE = "rule_approve"
    EXPORT = "export"
    AUDIT = "audit"
    RECIPIENT = "recipient"


ROLE_PERMISSIONS = {
    "safety_admin": frozenset(
        {
            Permission.READ,
            Permission.ADMIN,
            Permission.CAPTURE,
            Permission.REVIEW,
            Permission.DISPATCH,
            Permission.RECHECK,
            Permission.EXPORT,
            Permission.AUDIT,
        }
    ),
    "lab_manager": frozenset({Permission.READ, Permission.DISPATCH, Permission.EXPORT}),
    "inspector": frozenset(
        {Permission.READ, Permission.CAPTURE, Permission.REVIEW, Permission.RECHECK}
    ),
    "remediator": frozenset({Permission.READ, Permission.ASSIGNEE}),
    "viewer": frozenset({Permission.READ}),
    "rule_expert": frozenset({Permission.READ, Permission.RULE_APPROVE}),
}
ROLE_SCOPES = {
    "safety_admin": "tenant",
    "rule_expert": "tenant",
    "lab_manager": "laboratory",
    "inspector": "laboratory",
    "remediator": "laboratory",
    "viewer": "laboratory",
}


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    username: str
    roles: tuple[tuple[str, str, str | None], ...]


def valid_grant(role: str, scope: str, lab: str | None) -> bool:
    return ROLE_SCOPES.get(role) == scope and (
        (scope == "tenant" and lab is None) or (scope == "laboratory" and bool(lab))
    )


def permission_projection(principal: Principal) -> list[dict[str, str | None]]:
    permissions = sorted(
        {
            (str(permission), lab or "")
            for role, scope, lab in principal.roles
            if valid_grant(role, scope, lab)
            for permission in ROLE_PERMISSIONS[role]
        }
    )
    if len(permissions) > 200:
        raise ServiceError("STATE_CONFLICT", 409, "Permission projection exceeds capacity")
    return [{"action": action, "laboratory_id": lab or None} for action, lab in permissions]


def visible_laboratories(principal: Principal) -> frozenset[str] | None:
    """None means all labs in this tenant, never all tenants."""
    labs = set()
    for role, scope, lab in principal.roles:
        if not valid_grant(role, scope, lab):
            continue
        if role == "safety_admin":
            return None
        if scope == "laboratory":
            labs.add(lab)
    return frozenset(labs)


def authorize(
    principal: Principal,
    permission: Permission | str,
    laboratory_id: str | None = None,
    *,
    assignee_id: str | None = None,
    submitted_by: str | None = None,
    creator_id: str | None = None,
    recipient_id: str | None = None,
    published: bool = False,
) -> None:
    permission = Permission(permission)
    grants = tuple(g for g in principal.roles if valid_grant(*g))
    if permission == Permission.RECIPIENT:
        if recipient_id == principal.user_id:
            return
        raise not_found()
    if laboratory_id is not None:
        labs = visible_laboratories(principal)
        if labs is not None and laboratory_id not in labs:
            raise not_found()
    for role, scope, lab in grants:
        if permission not in ROLE_PERMISSIONS[role]:
            continue
        if permission == Permission.READ and published and laboratory_id is None:
            return  # Caller must supply only a non-sensitive published projection.
        if scope == "laboratory" and (laboratory_id is None or lab != laboratory_id):
            continue
        if role == "rule_expert" and permission == Permission.READ:
            continue
        if permission == Permission.ASSIGNEE and assignee_id != principal.user_id:
            continue
        if permission == Permission.RULE_APPROVE and (
            submitted_by is None or submitted_by == principal.user_id
        ):
            continue
        if permission == Permission.RECHECK and principal.user_id in (assignee_id, submitted_by):
            continue
        if permission == Permission.CAPTURE and creator_id is not None:
            if role != "safety_admin" and creator_id != principal.user_id:
                continue
        return
    raise ServiceError("FORBIDDEN", 403, "Forbidden")


def require_version(actual: int, expected: int) -> None:
    if type(expected) is not int or not 1 <= expected <= 2147483647:
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid expected_version")
    if actual != expected:
        raise ServiceError("VERSION_CONFLICT", 409, "Refresh the resource before retrying")
