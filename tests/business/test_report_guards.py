"""Range limits and persisted report dispatch protocol."""

from copy import deepcopy
from uuid import uuid4

import pytest

from packages.domain.reports import validate_request
from packages.domain.security import ServiceError
from packages.persistence.dispatch import validate_dispatch
from tests.business.test_dispatch import message
from tests.business.test_report_boundary import BODY


def test_export_31_days_and_100_labs_are_accepted_in_utc():
    request = deepcopy(BODY)
    request["filters"].update(
        {
            "laboratory_ids": [str(uuid4()) for _ in range(100)],
            "from": "2030-01-01T08:00:00+08:00",
            "to": "2030-02-01T00:00:00Z",
        }
    )
    normalized, start, end = validate_request(request)
    assert normalized["from"] == "2030-01-01T00:00:00.000Z"
    assert (end - start).days == 31


@pytest.mark.parametrize(
    "field,value",
    [
        ("laboratory_ids", []),
        ("laboratory_ids", [str(uuid4()) for _ in range(101)]),
        ("laboratory_ids", ["bad"]),
        ("laboratory_ids", BODY["filters"]["laboratory_ids"] * 2),
        ("from", "2030-01-01"),
        ("to", "2029-12-31T00:00:00Z"),
        ("to", "2030-02-01T00:00:00.001Z"),
        ("severity", ["wrong"]),
        ("finding_status", ["confirmed", "confirmed"]),
    ],
)
def test_export_invalid_ranges_fail_before_persistence(field, value):
    request = deepcopy(BODY)
    request["filters"][field] = value
    with pytest.raises(ServiceError) as error:
        validate_request(request)
    assert error.value.status == 422


@pytest.mark.parametrize("fault", [None, "missing", "extra", "mismatched", "invalid"])
def test_report_dispatch_requires_exact_export_binding(fault):
    value = message()
    value.update(task_type="report_export", payload={"export_id": value["resource_id"]})
    if fault == "missing":
        value["payload"] = {}
    elif fault == "extra":
        value["payload"]["object_key"] = "not-allowed"
    elif fault == "mismatched":
        value["payload"]["export_id"] = str(uuid4())
    elif fault == "invalid":
        value["payload"]["export_id"] = value["resource_id"] = "bad"
    if fault:
        with pytest.raises((ValueError, ServiceError)):
            validate_dispatch(value)
    else:
        assert validate_dispatch(value) == value
