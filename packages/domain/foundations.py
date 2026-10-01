"""Pure guards for location structure, template bodies and inspection creation."""

from packages.domain.security import Permission, ServiceError, authorize


def require_active(status):
    if status != "active":
        raise ServiceError("STATE_CONFLICT", 409, "Resource is not active")


def location_parent(kind, parent_kind):
    allowed = {
        "room": {None},
        "area": {"room"},
        "shelf": {"room", "area"},
        "cabinet": {"room", "area"},
    }
    if kind not in allowed or parent_kind not in allowed[kind]:
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid location parent type")


def template_items(items):
    if not 1 <= len(items) <= 100:
        raise ServiceError("VALIDATION_ERROR", 422, "Template needs 1 to 100 items")
    for key in ("id", "code", "sort_order"):
        if len({item[key] for item in items}) != len(items):
            raise ServiceError("VALIDATION_ERROR", 422, "Duplicate template item field")


def create_inspection(actor, laboratory_id, template_status, item_count, location_ids):
    authorize(actor, Permission.CAPTURE, laboratory_id)
    if template_status != "published":
        raise ServiceError("STATE_CONFLICT", 409, "Template is not published")
    if (
        not 1 <= len(location_ids) <= 100
        or len(set(location_ids)) != len(location_ids)
        or not 1 <= item_count <= 100
        or item_count * len(location_ids) > 100
    ):
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid inspection item capacity")
