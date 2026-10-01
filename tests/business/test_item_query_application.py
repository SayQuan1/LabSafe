"""Read transaction boundaries, authoritative actor reload and bounded retries."""

from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from packages.application.item_queries import ItemQueryApplication
from packages.domain.security import ServiceError
from tests.business.test_identity_application import deadlock


def application():
    identity = Mock()
    connection = object()
    active = False

    @contextmanager
    def transaction():
        nonlocal active
        assert not active
        active = True
        try:
            yield connection
        finally:
            active = False

    def limit(actor, *, write):
        assert actor is identity._preflight.return_value
        assert not active and write is False

    def authenticate(conn, token):
        assert active and conn is connection and token == "token"
        return Mock(principal="reloaded-actor")

    identity.transaction.side_effect = transaction
    identity.limits.user.side_effect = limit
    identity.sessions.authenticate.side_effect = authenticate
    app = ItemQueryApplication(identity)
    app.items = Mock()
    return app, identity, connection


@pytest.mark.parametrize("operation", ["list", "get"])
def test_read_uses_reauthenticated_actor_and_transaction_local_projection(operation):
    app, identity, connection = application()
    getattr(app.items, operation).return_value = {"item": "projection"}
    result = app.read(
        operation,
        "token",
        "request",
        resource_id="resource",
        laboratory_id="lab",
        page=3,
        page_size=5,
    )
    assert result == {"data": {"item": "projection"}, "request_id": "request"}
    identity._preflight.assert_called_once_with("token")
    identity.sessions.authenticate.assert_called_once_with(connection, "token")
    arguments = (connection, "reloaded-actor", "resource")
    if operation == "list":
        arguments += (3, 5, "lab")
    getattr(app.items, operation).assert_called_once_with(*arguments)


def test_deadlock_reauthenticates_and_retries_entire_read_without_recharging_limit():
    app, identity, _ = application()
    app.items.get.side_effect = [deadlock(), {"id": "item"}]
    assert app.read("get", "token", "request", resource_id="item")["data"] == {"id": "item"}
    assert identity.transaction.call_count == identity.sessions.authenticate.call_count == 2
    assert app.items.get.call_args_list[0] == app.items.get.call_args_list[1]
    identity.limits.user.assert_called_once()


def test_deadlock_exhaustion_is_bounded_and_keeps_retry_hint():
    app, identity, _ = application()
    app.items.get.side_effect = deadlock()
    with pytest.raises(ServiceError) as caught:
        app.read("get", "token", "request", resource_id="item")
    assert caught.value.status == 409 and caught.value.retry_after == 2
    assert identity.transaction.call_count == 3
    identity.limits.user.assert_called_once()


def test_rate_limit_rejection_never_starts_read_transaction():
    app, identity, _ = application()
    identity.limits.user.side_effect = ServiceError("RATE_LIMITED", 429, "Too many requests")
    with pytest.raises(ServiceError) as caught:
        app.read("get", "token", "request", resource_id="item")
    assert caught.value.status == 429
    identity.transaction.assert_not_called()
    identity.sessions.authenticate.assert_not_called()
    assert not app.items.mock_calls


def test_revoked_session_does_not_reuse_preflight_principal():
    app, identity, _ = application()
    identity.sessions.authenticate.side_effect = ServiceError("UNAUTHENTICATED", 401, "Revoked")
    with pytest.raises(ServiceError) as caught:
        app.read("list", "token", "request", resource_id="item")
    assert caught.value.status == 401
    assert not app.items.mock_calls


def test_unknown_internal_operation_is_not_silently_mapped_to_get():
    app, _, _ = application()
    with pytest.raises(ValueError, match="Unknown inspection item query"):
        app.read("typo", "token", "request", resource_id="item")
    assert not app.items.mock_calls
