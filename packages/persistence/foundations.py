"""Shared SQL helpers for fixed, server-owned foundation queries."""

from sqlalchemy import text

from packages.domain.security import Permission, ServiceError, authorize, visible_laboratories
from packages.persistence.bootstrap import normalized
from packages.persistence.users import timestamp

BASE = "id,created_at,updated_at,version"


def project(row):
    result = dict(row)
    for key in ("created_at", "updated_at"):
        if key in result:
            result[key] = timestamp(result[key])
    return result


def clean(value, maximum):
    try:
        return normalized(value, maximum)
    except (ValueError, TypeError):
        raise ServiceError("VALIDATION_ERROR", 422, "Invalid resource text") from None


def is_admin(actor):
    try:
        authorize(actor, Permission.ADMIN)
        return True
    except ServiceError:
        return False


def lab_filter(actor, column, params, laboratory_id=None):
    if laboratory_id is not None:
        authorize(actor, Permission.READ, laboratory_id)
        params["filter_lab"] = laboratory_id
        return f" AND {column}=:filter_lab"
    labs = visible_laboratories(actor)
    if labs is None:
        return ""
    if not labs:
        raise ServiceError("FORBIDDEN", 403, "No laboratory access")
    names = []
    for index, lab in enumerate(sorted(labs)):
        key = f"scope_{index}"
        params[key] = lab
        names.append(":" + key)
    return f" AND {column} IN ({','.join(names)})"


def page_query(
    connection,
    table,
    columns,
    where,
    params,
    page,
    page_size,
    *,
    project_row=project,
    order_by="created_at DESC,id DESC",
):
    # SQL fragments are fixed by repositories, never supplied by the request.
    total = connection.scalar(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params)
    rows = (
        connection.execute(
            text(
                f"SELECT {columns} FROM {table} WHERE {where} "
                f"ORDER BY {order_by} LIMIT :limit OFFSET :offset"
            ),
            {**params, "limit": page_size, "offset": (page - 1) * page_size},
        )
        .mappings()
        .all()
    )
    return {
        "items": [project_row(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
