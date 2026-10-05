"""Protocol rejection and bounded broker-process behavior."""

import copy
import json
import multiprocessing
import time
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from apps.worker.dispatch import CeleryDispatchTransport, main, send_dispatch
from packages.domain.security import ServiceError
from packages.persistence.dispatch import validate_dispatch


def message():
    image = str(uuid4())
    return {
        "schema_version": "1.1",
        "tenant_id": str(uuid4()),
        "trace_id": uuid4().hex,
        "task_id": str(uuid4()),
        "task_type": "validate_image",
        "resource_id": image,
        "replay_generation": 0,
        "dispatch_sequence": 1,
        "created_at": "2026-10-01T00:00:00Z",
        "payload": {"upload_id": str(uuid4()), "image_id": image},
    }


def test_implemented_message_matches_canonical_contract():
    spec = json.loads(
        (Path(__file__).resolve().parents[2] / "contracts/task-message-v1.json").read_text()
    )
    value = validate_dispatch(message())
    Draft202012Validator(spec, format_checker=FormatChecker()).validate(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "1.0"),
        ("task_type", "inference_pipeline"),
        ("tenant_id", "other"),
        ("task_id", 5),
        ("resource_id", str(uuid4())),
        ("trace_id", "X" * 32),
        ("trace_id", None),
        ("created_at", "2026-10-01"),
        ("replay_generation", -1),
        ("replay_generation", True),
        ("dispatch_sequence", 0),
        ("dispatch_sequence", 2147483648),
        ("payload", {}),
        ("payload", None),
    ],
)
def test_bad_dispatch_never_validates(field, value):
    request = message()
    request[field] = value
    with pytest.raises((ValueError, ServiceError)):
        validate_dispatch(request)


def test_rule_evaluation_dispatch_validates():
    run_id, fact_id, bundle_id = str(uuid4()), str(uuid4()), str(uuid4())
    value = message()
    value.update(
        task_type="rule_evaluation",
        resource_id=run_id,
        payload={
            "run_id": run_id,
            "fact_revision_id": fact_id,
            "rule_bundle_id": bundle_id,
        },
    )
    assert validate_dispatch(value)["task_type"] == "rule_evaluation"


@pytest.mark.parametrize("path", ["schema_version", "payload", "tenant_id"])
def test_missing_fields_rejected(path):
    value = message()
    del value[path]
    with pytest.raises(ValueError):
        validate_dispatch(value)


def test_extra_fields_rejected():
    value = message()
    extra = copy.deepcopy(value)
    extra["broker_url"] = "secret"
    with pytest.raises(ValueError):
        validate_dispatch(extra)
    value["payload"]["object_key"] = "untrusted"
    with pytest.raises(ValueError):
        validate_dispatch(value)


def test_send_uses_stable_event_id_and_explicit_general_queue():
    app = Mock()
    value, event_id = message(), str(uuid4())
    with patch("apps.worker.dispatch.create_celery_app", return_value=app):
        send_dispatch(event_id, value)
    app.send_task.assert_called_once_with(
        "labsafe.tasks.dispatch",
        args=[value],
        task_id=event_id,
        queue="q.general",
        serializer="json",
        retry=False,
        headers={"outbox_event_id": event_id},
    )
    app.close.assert_called_once()


def test_child_suppresses_broker_secret_exception():
    with patch("apps.worker.dispatch.create_celery_app", side_effect=ValueError("secret")):
        with pytest.raises(SystemExit) as error:
            send_dispatch(str(uuid4()), message())
    assert error.value.code == 1
    assert error.value.__suppress_context__


@pytest.mark.parametrize("mode", ["success", "timeout", "failed", "lease_lost"])
def test_transport_reaps_children_and_enforces_deadline(mode):
    child = Mock(exitcode=0 if mode != "failed" else 1)
    child.is_alive.side_effect = {
        "success": [False, False, False],
        "failed": [False, False, False],
        "timeout": [True, True, True],
        "lease_lost": [True, True],
    }[mode]
    renew = Mock(side_effect=RuntimeError("lost") if mode == "lease_lost" else None)
    with patch("apps.worker.dispatch.multiprocessing.get_context") as context:
        context.return_value.Process.return_value = child
        if mode == "success":
            CeleryDispatchTransport().publish(str(uuid4()), message(), renew)
        else:
            with pytest.raises(RuntimeError):
                CeleryDispatchTransport().publish(str(uuid4()), message(), renew)
    child.start.assert_called_once()
    child.close.assert_called_once()
    if mode in {"timeout", "lease_lost"}:
        child.kill.assert_called_once()
        renew.assert_called_once()
    else:
        child.kill.assert_not_called()


@pytest.mark.parametrize("environment,enabled", [("production", "1"), ("test", "0")])
def test_cli_rejects_before_opening_db(environment, enabled, monkeypatch):
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("WORKER_DISPATCH_ENABLED", enabled)
    with patch("sys.argv", ["dispatch", "publisher", "--once"]):
        with patch("apps.worker.dispatch.database_engine") as engine:
            with pytest.raises((SystemExit, ValueError, RuntimeError)):
                main()
            engine.assert_not_called()


def stalled_broker(*_args):
    time.sleep(60)


def test_real_stalled_child_is_killed_and_joined(monkeypatch):
    monkeypatch.setattr("apps.worker.dispatch.PUBLISH_SECONDS", 0.3)
    monkeypatch.setattr("apps.worker.dispatch.HEARTBEAT_SECONDS", 0.1)
    monkeypatch.setattr("apps.worker.dispatch.send_dispatch", stalled_broker)
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="unconfirmed"):
        CeleryDispatchTransport().publish(str(uuid4()), message(), lambda: None)
    assert time.monotonic() - started < 5
    assert {child.pid for child in multiprocessing.active_children()} == before
