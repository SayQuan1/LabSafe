"""Application retry boundaries and readiness do not require a live database."""

from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy.exc import OperationalError

from packages.application.identity import IdentityApplication, retry_deadlocks
from packages.domain.security import ServiceError


def deadlock(errno=1213):
    return OperationalError("test statement", {}, Exception(errno, "test error"))


def test_deadlock_retries_whole_command_without_changing_version():
    command = Mock(side_effect=[deadlock(), deadlock(), "committed"])
    assert retry_deadlocks(command)(expected_version=7) == "committed"
    assert command.call_count == 3
    assert all(call.kwargs == {"expected_version": 7} for call in command.call_args_list)


def test_deadlock_retries_stop_after_three_attempts():
    command = Mock(side_effect=deadlock())
    with pytest.raises(ServiceError) as error:
        retry_deadlocks(command)()
    assert command.call_count == 3
    assert error.value.code == "REQUEST_IN_PROGRESS" and error.value.retry_after == 2


@pytest.mark.parametrize("errno", [1205, 2003, 3572])
def test_non_deadlock_database_errors_are_not_retried(errno):
    command = Mock(side_effect=deadlock(errno))
    with pytest.raises(OperationalError):
        retry_deadlocks(command)()
    assert command.call_count == 1


@pytest.mark.parametrize(
    "revision", ["0002_report_object_version", "0001_initial", "unknown", None]
)
def test_readiness_checks_revision_and_releases_connection_before_redis(revision):
    connected = False
    connection = Mock()
    connection.scalar.return_value = revision

    @contextmanager
    def connect():
        nonlocal connected
        connected = True
        try:
            yield connection
        finally:
            connected = False

    def ready():
        assert not connected

    engine, limits = Mock(), Mock()
    engine.connect.side_effect = connect
    limits.ready.side_effect = ready
    app = IdentityApplication(engine, Mock(), limits, "test")
    if revision == "0002_report_object_version":
        app.readiness()
        limits.ready.assert_called_once_with()
    else:
        with pytest.raises(ServiceError) as error:
            app.readiness()
        assert error.value.status == 503
        limits.ready.assert_not_called()
