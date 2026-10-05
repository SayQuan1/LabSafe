import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from packages.application.job_execution import ImageExecution, LeaseHeartbeat
from packages.domain.job_execution import TECHNICAL_ERRORS, LeaseLost, retry_delay


@pytest.mark.parametrize("attempt,seconds", [(1, 5), (2, 30), (3, 120)])
@pytest.mark.parametrize("jitter", [0, 0.1, 0.2])
def test_retry_delay_matches_policy(attempt, seconds, jitter):
    actual = retry_delay(attempt, uniform=lambda low, high: jitter)
    assert actual.total_seconds() == seconds * (1 + jitter)


@pytest.mark.parametrize("attempt", [0, 4, True, -1, "1"])
def test_retry_delay_refuses_invalid_attempt(attempt):
    with pytest.raises(ValueError):
        retry_delay(attempt)


def test_failure_classification_matches_authoritative_contract():
    contract = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/error-codes.json").read_text()
    )
    assert all(
        contract[code]["retryable"] == retryable for code, retryable in TECHNICAL_ERRORS.items()
    )


@pytest.mark.parametrize("failure", [False, RuntimeError("secret database URL")])
def test_heartbeat_failure_latches_cancels_and_prevents_success_exit(failure, caplog):
    service, cancel = Mock(), Mock()
    if isinstance(failure, Exception):
        service.heartbeat.side_effect = failure
    else:
        service.heartbeat.return_value = failure
    keeper = LeaseHeartbeat(service, SimpleNamespace(task_id="test-task"), cancel)
    keeper.interval = 0.01
    with pytest.raises(LeaseLost):
        with keeper:
            assert keeper.lost.wait(2)
    cancel.assert_called_once()
    assert "secret database" not in caplog.text
    assert not keeper.thread.is_alive()


def test_heartbeat_clean_exit_stops_thread():
    service = Mock()
    keeper = LeaseHeartbeat(service, SimpleNamespace(task_id="test-task"), Mock())
    with keeper:
        pass
    assert not keeper.thread.is_alive()
    service.heartbeat.assert_not_called()


def test_execution_is_not_released_for_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError):
        ImageExecution(Mock())


@pytest.mark.parametrize("cancel_raises", [False, True])
def test_execute_never_commits_after_transient_heartbeat_failure(monkeypatch, cancel_raises):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setattr(LeaseHeartbeat, "interval", 0.01)
    execution = ImageExecution(Mock())
    execution.claim = Mock(return_value=SimpleNamespace(task_id="test-task"))
    execution.heartbeat = Mock(side_effect=[False, True])
    execution.commit = Mock()
    execution.fail = Mock()

    def external_work(input, cancelled):
        assert isinstance(cancelled, Event)
        assert cancelled.wait(2)
        if cancel_raises:
            raise RuntimeError("external work cancelled")
        return "discard-me"

    execution.claim.return_value.input = None
    with pytest.raises(LeaseLost):
        execution.execute({}, external_work, Mock())
    execution.commit.assert_not_called()
    execution.fail.assert_not_called()
