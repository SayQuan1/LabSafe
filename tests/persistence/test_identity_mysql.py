"""Real HTTP -> application -> MySQL/Redis tests on disposable local/CI services."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from redis.exceptions import ConnectionError
from sqlalchemy import text

from packages.domain.security import ServiceError
from packages.persistence.security import (
    begin_idempotency,
    complete_idempotency,
    revoke_user_sessions,
    utc_now,
)
from tests.persistence.test_security_mysql import PASSWORD, create_tenant

ORIGIN = "https://labsafe.test"


def login(http, tenant, *, username="admin", password=PASSWORD):
    return http.post(
        "/api/v1/auth/login",
        json={"tenant_code": tenant["code"], "username": username, "password": password},
        headers={"Origin": ORIGIN},
    )


def headers(session, key=None):
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": session["data"]["csrf_token"],
        "Idempotency-Key": key or str(uuid4()),
    }


def user_body(name="newuser"):
    return {"username": name, "display_name": "Test user", "initial_password": PASSWORD}


def assert_schema(response, schema_name):
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "contracts/public-api-v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": spec["components"]}
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(response.json()["data"])
    assert set(response.json()) == {"data", "request_id"}


def test_http_login_me_create_replay_disable_logout(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant)
        assert session.status_code == 200, session.text
        assert_schema(session, "Session")
        assert_schema(http.get("/api/v1/me"), "Session")
        h = headers(session.json())
        response = http.post("/api/v1/users", json=user_body(), headers=h)
        assert response.status_code == 201, response.text
        assert_schema(response, "User")
        user = response.json()["data"]
        replay = http.post("/api/v1/users", json=user_body(), headers=h)
        assert replay.json() == response.json() and replay.status_code == 201
        changed = http.post("/api/v1/users", json=user_body("different"), headers=h)
        assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        assert_schema(http.get("/api/v1/users"), "UserPage")
        assert_schema(http.get(f"/api/v1/users/{user['id']}"), "User")
        disable = {"expected_version": 1, "reason": "test disable"}
        disabled = http.post(
            f"/api/v1/users/{user['id']}/disable", json=disable, headers=headers(session.json())
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["data"]["version"] == 2
        with database.begin() as connection:
            count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM audit_events WHERE tenant_id=:tenant "
                    "AND action='user.create'"
                ),
                {"tenant": tenant["tenant_id"]},
            )
            assert count == 1
            records = connection.execute(
                text("SELECT request_hash,response FROM api_idempotency WHERE tenant_id=:tenant"),
                {"tenant": tenant["tenant_id"]},
            ).all()
            assert PASSWORD not in repr(records)
        out = http.post("/api/v1/auth/logout", json={}, headers=headers(session.json()))
        assert out.status_code == 200, out.text
        assert_schema(out, "Ack")
        assert http.get("/api/v1/me").status_code == 401


def test_last_admin_cross_tenant_and_csrf_protection(identity, database):
    tenant, _, app = identity
    other = create_tenant(database, "foreign")
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        assert http.get(f"/api/v1/users/{other['admin_id']}").status_code == 404
        own = http.post(
            f"/api/v1/users/{tenant['admin_id']}/disable",
            json={"expected_version": 1, "reason": "last admin"},
            headers=headers(session),
        )
        assert own.status_code == 409, own.text
        assert own.json()["error"]["code"] == "STATE_CONFLICT"
        h = headers(session)
        h["X-CSRF-Token"] = "x" * 43
        assert http.post("/api/v1/users", json=user_body(), headers=h).status_code == 403
        assert http.get("/api/v1/me").status_code == 200


def test_login_rate_limits_exact_failed_attempts_and_success_release(identity):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        for _ in range(6):
            assert login(http, tenant).status_code == 200
        for _ in range(5):
            failed = login(http, tenant, password="wrong-test-password")
            assert failed.status_code == 401, failed.text
        blocked = login(http, tenant)
        assert blocked.status_code == 429
        assert 1 <= int(blocked.headers["Retry-After"]) <= 900


def test_disabled_sessions_and_revoked_replay_are_denied(identity, database):
    tenant, service, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        h = headers(session)
        assert http.post("/api/v1/users", json=user_body(), headers=h).status_code == 201
        with database.begin() as connection:
            revoke_user_sessions(connection, tenant["tenant_id"], tenant["admin_id"])
        assert http.post("/api/v1/users", json=user_body(), headers=h).status_code == 401
        assert http.get("/api/v1/me").status_code == 401
        assert service.environment == "test"


def test_user_change_audit_failure_rolls_back_business_and_idempotency(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        with patch(
            "packages.persistence.users.append_audit", side_effect=RuntimeError("audit failed")
        ):
            result = http.post("/api/v1/users", json=user_body(), headers=headers(session))
        assert result.status_code == 500
        with database.begin() as connection:
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM users WHERE tenant_id=:tenant"),
                    {"tenant": tenant["tenant_id"]},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text("SELECT COUNT(*) FROM api_idempotency WHERE tenant_id=:tenant"),
                    {"tenant": tenant["tenant_id"]},
                )
                == 0
            )


def test_same_key_ten_requests_create_one_user_and_audit(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        cookie = http.cookies.get("labsafe_session")
    gate = Barrier(10)
    h = {**headers(session), "Cookie": f"labsafe_session={cookie}"}

    def send():
        with TestClient(app, base_url=ORIGIN) as http:
            gate.wait(timeout=10)
            return http.post("/api/v1/users", json=user_body(), headers=h)

    with ThreadPoolExecutor(max_workers=10) as pool:
        replies = list(pool.map(lambda _: send(), range(10)))
    successes = [r for r in replies if r.status_code == 201]
    assert successes, [r.text for r in replies]
    assert all(r.status_code in {201, 409} for r in replies), [r.text for r in replies]
    assert len({r.text for r in successes}) == 1
    with TestClient(app, base_url=ORIGIN) as http:
        assert http.post("/api/v1/users", json=user_body(), headers=h).json() == successes[0].json()
    with database.begin() as connection:
        for query, count in [
            ("SELECT COUNT(*) FROM users WHERE tenant_id=:tenant", 2),
            (
                "SELECT COUNT(*) FROM audit_events WHERE tenant_id=:tenant "
                "AND action='user.create'",
                1,
            ),
            ("SELECT COUNT(*) FROM api_idempotency WHERE tenant_id=:tenant", 1),
        ]:
            assert connection.scalar(text(query), {"tenant": tenant["tenant_id"]}) == count


def test_same_version_race_has_single_success(identity):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        user = http.post("/api/v1/users", json=user_body(), headers=headers(session)).json()["data"]
        cookie = http.cookies.get("labsafe_session")
    gate = Barrier(2)

    def disable():
        with TestClient(app, base_url=ORIGIN) as http:
            gate.wait(timeout=10)
            return http.post(
                f"/api/v1/users/{user['id']}/disable",
                json={"expected_version": 1, "reason": "race"},
                headers={**headers(session), "Cookie": f"labsafe_session={cookie}"},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: disable(), range(2)))
    assert sorted(r.status_code for r in replies) == [200, 409], [r.text for r in replies]
    assert (
        next(r for r in replies if r.status_code == 409).json()["error"]["code"]
        == "VERSION_CONFLICT"
    )


def test_identity_transactions_enforce_repeatable_read_and_redis_stays_outside(identity):
    tenant, service, app = identity
    active = 0
    transaction = service.transaction
    original_eval = service.limits.redis.eval

    @contextmanager
    def tracked_transaction():
        nonlocal active
        with transaction() as connection:
            active += 1
            try:
                isolation = connection.scalar(text("SELECT @@SESSION.transaction_isolation"))
                assert isolation == "REPEATABLE-READ"
                yield connection
            finally:
                active -= 1

    def eval_outside(*args, **kwargs):
        assert active == 0
        return original_eval(*args, **kwargs)

    with (
        patch.object(service, "transaction", side_effect=tracked_transaction),
        patch.object(service.limits.redis, "eval", side_effect=eval_outside),
        TestClient(app, base_url=ORIGIN) as http,
    ):
        session = login(http, tenant).json()
        assert http.get("/api/v1/users").status_code == 200
        response = http.post("/api/v1/users", json=user_body(), headers=headers(session))
        assert response.status_code == 201


def test_login_release_failure_never_delivers_a_cookie(identity, database):
    tenant, service, app = identity
    failure = ServiceError("DEPENDENCY_UNAVAILABLE", 503, "Rate limiter unavailable")
    with TestClient(app, base_url=ORIGIN) as http:
        with patch.object(service.limits, "login_succeeded", side_effect=failure):
            response = login(http, tenant)
        assert response.status_code == 503 and "set-cookie" not in response.headers
        assert http.cookies.get("labsafe_session") is None
    # MySQL committed before Redis released the slot; no false cross-system atomicity.
    with database.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM sessions WHERE tenant_id=:tenant"),
                {"tenant": tenant["tenant_id"]},
            )
            == 1
        )
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM audit_events "
                    "WHERE tenant_id=:tenant AND action='session.login'"
                ),
                {"tenant": tenant["tenant_id"]},
            )
            == 1
        )


def test_live_pending_and_expired_owner_cannot_commit(database):
    tenant = create_tenant(database, "lease")
    args = dict(
        tenant_id=tenant["tenant_id"],
        actor_id=tenant["admin_id"],
        method="POST",
        path="/api/v1/users",
        key="lease-test-key",
        request_hash="a" * 64,
        expires_at=utc_now() + timedelta(hours=24),
    )
    with database.begin() as connection:
        claim = begin_idempotency(connection, **args, lease_owner=str(uuid4()))
    with database.begin() as connection:
        with pytest.raises(ServiceError) as error:
            begin_idempotency(connection, **args, lease_owner=str(uuid4()))
        assert error.value.code == "REQUEST_IN_PROGRESS"
        connection.execute(
            text("UPDATE api_idempotency SET lease_until=:past WHERE id=:id"),
            {"past": utc_now() - timedelta(seconds=1), "id": claim.record_id},
        )
        with pytest.raises(ServiceError):
            complete_idempotency(connection, claim, status=200, response={})
        takeover = begin_idempotency(connection, **args, lease_owner=str(uuid4()))
        with pytest.raises(ServiceError):
            complete_idempotency(connection, claim, status=200, response={})
        complete_idempotency(connection, takeover, status=200, response={"data": {"ok": True}})


def test_real_dependencies_ready_and_redis_failure_has_no_write_side_effects(identity, database):
    tenant, service, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        assert http.get("/ready").status_code == 200
        session = login(http, tenant).json()
        with (
            patch.object(service.limits.redis, "eval", side_effect=ConnectionError("private URL")),
            patch.object(service.limits.redis, "ping", side_effect=ConnectionError("private URL")),
        ):
            assert http.get("/ready").status_code == 503
            assert http.get("/health").status_code == 200
            assert login(http, tenant).status_code == 503
            assert http.get("/api/v1/me").status_code == 200
            response = http.post("/api/v1/users", json=user_body(), headers=headers(session))
            assert response.status_code == 503 and "private" not in response.text
        assert http.get("/ready").status_code == 200
    with database.begin() as connection:
        for table, expected in [("users", 1), ("sessions", 1), ("api_idempotency", 0)]:
            assert (
                connection.scalar(
                    text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id=:tenant"),
                    {"tenant": tenant["tenant_id"]},
                )
                == expected
            )


@pytest.mark.parametrize("revocation", ["session", "permission"])
def test_preflight_does_not_authorize_after_session_or_permission_revocation(
    identity, database, revocation
):
    tenant, service, app = identity
    original_limit = service.limits.user

    def revoke_after_preflight(principal, *, write):
        original_limit(principal, write=write)
        # This commits between preflight and the command, with no open app transaction.
        with database.begin() as connection:
            if revocation == "session":
                revoke_user_sessions(connection, tenant["tenant_id"], tenant["admin_id"])
            else:
                connection.execute(
                    text("DELETE FROM user_roles WHERE tenant_id=:tenant AND user_id=:user"),
                    {"tenant": tenant["tenant_id"], "user": tenant["admin_id"]},
                )

    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        with patch.object(service.limits, "user", side_effect=revoke_after_preflight):
            response = http.post("/api/v1/users", json=user_body(), headers=headers(session))
        assert response.status_code == (401 if revocation == "session" else 403), response.text
    with database.begin() as connection:
        for table, expected in [("users", 1), ("api_idempotency", 0)]:
            assert (
                connection.scalar(
                    text(f"SELECT COUNT(*) FROM {table} WHERE tenant_id=:tenant"),
                    {"tenant": tenant["tenant_id"]},
                )
                == expected
            )


def test_unprivileged_user_cannot_use_administrative_routes(identity):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        admin = login(http, tenant).json()
        assert (
            http.post("/api/v1/users", json=user_body(), headers=headers(admin)).status_code == 201
        )
        session = login(http, tenant, username="newuser").json()
        assert http.get("/api/v1/me").status_code == 200
        assert http.get("/api/v1/users").status_code == 403
        assert http.get(f"/api/v1/users/{tenant['admin_id']}").status_code == 403
        assert (
            http.post(
                "/api/v1/users", json=user_body("notallowed"), headers=headers(session)
            ).status_code
            == 403
        )
        assert (
            http.post(
                f"/api/v1/users/{tenant['admin_id']}/disable",
                json={"expected_version": 1, "reason": "not allowed"},
                headers=headers(session),
            ).status_code
            == 403
        )


def test_normalized_username_collision_and_password_policy(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        collision = http.post(
            "/api/v1/users", json=user_body("ＡＤＭＩＮ"), headers=headers(session)
        )
        assert collision.status_code == 409, collision.text
        invalid = http.post(
            "/api/v1/users",
            json={**user_body(), "initial_password": "x" * 129},
            headers=headers(session),
        )
        assert invalid.status_code == 422 and "x" * 129 not in invalid.text
    with database.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM users WHERE tenant_id=:tenant"),
                {"tenant": tenant["tenant_id"]},
            )
            == 1
        )
        assert (
            connection.scalar(
                text("SELECT COUNT(*) FROM api_idempotency WHERE tenant_id=:tenant"),
                {"tenant": tenant["tenant_id"]},
            )
            == 0
        )


@pytest.mark.parametrize("disable_self", [True, False])
def test_two_admin_race_keeps_one_active_admin(identity, database, disable_self):
    tenant, service, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        first = login(http, tenant).json()
        first_token = http.cookies.get("labsafe_session")
        second = http.post(
            "/api/v1/users", json=user_body("admin2"), headers=headers(first)
        ).json()["data"]
        # Fixture-only grant: this batch deliberately exposes no role mutation API.
        with database.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_roles (id,tenant_id,user_id,role_id,scope_kind) "
                    "SELECT :id,:tenant,:user,id,'tenant' FROM roles WHERE code='safety_admin'"
                ),
                {"id": str(uuid4()), "tenant": tenant["tenant_id"], "user": second["id"]},
            )
        second_session = login(http, tenant, username="admin2").json()
        second_token = http.cookies.get("labsafe_session")
    gate = Barrier(2)
    original_limit = service.limits.user

    def synchronized_preflight(principal, *, write):
        original_limit(principal, write=write)
        if write:
            gate.wait(timeout=10)

    actors = [
        (tenant["admin_id"], first, first_token),
        (second["id"], second_session, second_token),
    ]

    def disable(index):
        actor, session, cookie = actors[index]
        target = actor if disable_self else actors[1 - index][0]
        with TestClient(app, base_url=ORIGIN) as http:
            return http.post(
                f"/api/v1/users/{target}/disable",
                json={"expected_version": 1, "reason": "admin race"},
                headers={**headers(session), "Cookie": f"labsafe_session={cookie}"},
            )

    with (
        patch.object(service.limits, "user", side_effect=synchronized_preflight),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        replies = list(pool.map(disable, range(2)))
    expected = [200, 409] if disable_self else [200, 401]
    assert sorted(r.status_code for r in replies) == expected, [r.text for r in replies]
    if disable_self:
        assert (
            next(r for r in replies if r.status_code == 409).json()["error"]["code"]
            == "STATE_CONFLICT"
        )
    with database.begin() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT COUNT(*) FROM users u JOIN user_roles ur ON ur.user_id=u.id "
                    "AND ur.tenant_id=u.tenant_id JOIN roles r ON r.id=ur.role_id "
                    "WHERE u.tenant_id=:tenant AND u.status='active' AND r.code='safety_admin'"
                ),
                {"tenant": tenant["tenant_id"]},
            )
            == 1
        )
