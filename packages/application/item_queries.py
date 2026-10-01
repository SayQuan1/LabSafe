"""Inspection item queries: preflight, external rate limit, authoritative read transaction."""

from packages.application.identity import retry_deadlocks
from packages.persistence.inspection_items import InspectionItemRepository


class ItemQueryApplication:
    def __init__(self, identity):
        self.identity = identity
        self.items = InspectionItemRepository()

    def read(
        self, operation, token, request_id, *, resource_id, laboratory_id=None, page=1, page_size=20
    ):
        principal = self.identity._preflight(token)
        self.identity.limits.user(principal, write=False)
        return self._read_once(
            operation,
            token,
            request_id,
            resource_id=resource_id,
            laboratory_id=laboratory_id,
            page=page,
            page_size=page_size,
        )

    @retry_deadlocks
    def _read_once(
        self, operation, token, request_id, *, resource_id, laboratory_id, page, page_size
    ):
        with self.identity.transaction() as connection:
            actor = self.identity.sessions.authenticate(connection, token).principal
            if operation == "get":
                data = self.items.get(connection, actor, resource_id)
            elif operation == "list":
                data = self.items.list(
                    connection, actor, resource_id, page, page_size, laboratory_id
                )
            else:
                raise ValueError("Unknown inspection item query")
            return {"data": data, "request_id": request_id}
