"""Role HTTP request shape, strict validation and route contract checks."""

from unittest.mock import Mock
from uuid import uuid4

import pytest

from tests.business.test_identity_boundary import ORIGIN, client

BODY = {
    "expected_version": 1,
    "role": "inspector",
    "scope_kind": "laboratory",
    "laboratory_id": str(uuid4()),
}
HEADERS = {"Origin": ORIGIN, "Idempotency-Key": "test-role-123", "X-CSRF-Token": "s" * 43}


@pytest.mark.parametrize(
    "suffix,operation", [("roles", "grant_role"), ("revoke-role", "revoke_role")]
)
def test_role_commands_pass_exact_contract_to_application(suffix, operation):
    service = Mock()
    service.write.return_value = (200, {"data": {}, "request_id": str(uuid4())})
    user = str(uuid4())
    with client(service) as http:
        result = http.post(f"/api/v1/users/{user}/{suffix}", json=BODY, headers=HEADERS)
        assert result.status_code == 200
    assert service.write.call_args.args[0] == operation
    assert service.write.call_args.args[4] == BODY
    assert service.write.call_args.kwargs == {"user_id": user}


@pytest.mark.parametrize(
    "patch",
    [
        {"role": "superadmin"},
        {"scope_kind": "global"},
        {"expected_version": True},
        {"expected_version": "1"},
        {"expected_version": 0},
        {"expected_version": 2147483648},
        {"laboratory_id": "bad"},
        {"laboratory_id": 4},
        {"tenant_id": str(uuid4())},
        {"reason": "not in role contract"},
    ],
)
def test_role_invalid_input_is_rejected_without_application_call(patch):
    service = Mock()
    with client(service) as http:
        for suffix in ("roles", "revoke-role"):
            result = http.post(
                f"/api/v1/users/{uuid4()}/{suffix}", json={**BODY, **patch}, headers=HEADERS
            )
            assert result.status_code == 422, result.text
    service.write.assert_not_called()


def test_role_nullable_lab_is_required_and_query_is_validated():
    service = Mock()
    service.read.return_value = {"data": {}, "request_id": str(uuid4())}
    user, lab = str(uuid4()), str(uuid4())
    with client(service) as http:
        missing = {k: v for k, v in BODY.items() if k != "laboratory_id"}
        assert (
            http.post(f"/api/v1/users/{user}/roles", json=missing, headers=HEADERS).status_code
            == 422
        )
        for query in (
            "laboratory_id=bad",
            "page=0",
            "page_size=101",
            "page=1&page=2",
            f"laboratory_id={lab}&laboratory_id={lab}",
            "role=viewer",
        ):
            assert http.get(f"/api/v1/users/{user}/roles?{query}").status_code == 422
        assert (
            http.get(
                f"/api/v1/users/{user}/roles?page=2&page_size=3&laboratory_id={lab}"
            ).status_code
            == 200
        )
    assert service.read.call_args.kwargs == {
        "user_id": user,
        "page": 2,
        "page_size": 3,
        "laboratory_id": lab,
    }


def test_roles_keep_origin_json_and_unique_write_header_requirements():
    service = Mock()
    with client(service) as http:
        for suffix in ("roles", "revoke-role"):
            path = f"/api/v1/users/{uuid4()}/{suffix}"
            assert (
                http.post(
                    path, json=BODY, headers={**HEADERS, "Origin": "https://evil.test"}
                ).status_code
                == 403
            )
            assert http.post(path, content="text", headers=HEADERS).status_code == 415
            assert http.post(path, json=BODY, headers={"Origin": ORIGIN}).status_code == 422
    service.write.assert_not_called()
