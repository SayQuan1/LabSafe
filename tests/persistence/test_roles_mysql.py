"""Role HTTP/transaction tests on the disposable MySQL and Redis instances."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.persistence.test_identity_mysql import ORIGIN, assert_schema, headers, login, user_body
from tests.persistence.test_security_mysql import create_tenant


def lab_fixture(database, tenant):
    college, lab = str(uuid4()), str(uuid4())
    with database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO colleges (id,tenant_id,code,name) "
                "VALUES (:id,:tenant,:id,'Synthetic college')"
            ),
            {"id": college, "tenant": tenant["tenant_id"]},
        )
        connection.execute(
            text(
                "INSERT INTO laboratories (id,tenant_id,college_id,code,name) "
                "VALUES (:id,:tenant,:college,:id,'Synthetic laboratory')"
            ),
            {"id": lab, "tenant": tenant["tenant_id"], "college": college},
        )
    return lab


def command(version=1, role="rule_expert", lab=None):
    return {
        "expected_version": version,
        "role": role,
        "scope_kind": "tenant" if lab is None else "laboratory",
        "laboratory_id": lab,
    }


def create_user(http, session, name="target"):
    response = http.post("/api/v1/users", json=user_body(name), headers=headers(session))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def role_write(http, session, user, body, *, revoke=False, key=None):
    suffix = "revoke-role" if revoke else "roles"
    return http.post(f"/api/v1/users/{user}/{suffix}", json=body, headers=headers(session, key))


def scalar(database, query, tenant, **params):
    with database.begin() as connection:
        return connection.scalar(text(query), {"tenant": tenant["tenant_id"], **params})


@pytest.mark.parametrize(
    "role", ["safety_admin", "rule_expert", "lab_manager", "inspector", "remediator", "viewer"]
)
def test_all_roles_grant_list_revoke_and_invalidate_all_sessions(identity, database, role):
    tenant, _, app = identity
    lab = None if role in {"safety_admin", "rule_expert"} else lab_fixture(database, tenant)
    with (
        TestClient(app, base_url=ORIGIN) as admin,
        TestClient(app, base_url=ORIGIN) as target,
        TestClient(app, base_url=ORIGIN) as other_session,
    ):
        session = login(admin, tenant).json()
        user = create_user(admin, session)
        assert login(target, tenant, username="target").status_code == 200
        assert login(other_session, tenant, username="target").status_code == 200
        before = scalar(
            database,
            "SELECT session_epoch FROM users WHERE tenant_id=:tenant AND id=:user",
            tenant,
            user=user["id"],
        )
        response = role_write(admin, session, user["id"], command(role=role, lab=lab))
        assert response.status_code == 200, response.text
        assert_schema(response, "User")
        assert response.json()["data"]["version"] == 2
        assert target.get("/api/v1/me").status_code == 401
        assert other_session.get("/api/v1/me").status_code == 401
        listed = admin.get(f"/api/v1/users/{user['id']}/roles")
        assert listed.status_code == 200
        assert_schema(listed, "RoleAssignmentPage")
        assignment = listed.json()["data"]["items"][0]
        assert listed.json()["data"]["total"] == 1
        assert assignment["role"] == role and assignment["laboratory_id"] == lab
        new_session = login(target, tenant, username="target")
        assert new_session.status_code == 200 and new_session.json()["data"]["permissions"]
        revoked = role_write(admin, session, user["id"], command(2, role, lab), revoke=True)
        assert revoked.status_code == 200 and revoked.json()["data"]["version"] == 3
        assert target.get("/api/v1/me").status_code == 401
        assert admin.get(f"/api/v1/users/{user['id']}/roles").json()["data"]["total"] == 0
        assert login(target, tenant, username="target").json()["data"]["permissions"] == []
        assert (
            scalar(
                database,
                "SELECT session_epoch FROM users WHERE tenant_id=:tenant AND id=:user",
                tenant,
                user=user["id"],
            )
            == before + 2
        )
        assert (
            scalar(
                database,
                "SELECT COUNT(*) FROM audit_events WHERE tenant_id=:tenant "
                "AND action IN ('role.grant','role.revoke') AND resource_id=:id",
                tenant,
                id=assignment["id"],
            )
            == 2
        )


def test_role_idempotency_duplicate_and_stale_version(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        user = create_user(http, session)
        key = str(uuid4())
        original = role_write(http, session, user["id"], command(), key=key)
        replay = role_write(http, session, user["id"], command(), key=key)
        assert original.status_code == replay.status_code == 200
        assert original.content == replay.content
        different = role_write(http, session, user["id"], command(1, "safety_admin"), key=key)
        assert different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
        duplicate = role_write(http, session, user["id"], command(2))
        assert duplicate.json()["error"]["code"] == "STATE_CONFLICT"
        stale = role_write(http, session, user["id"], command(), revoke=True)
        assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
        revoke_key = str(uuid4())
        revoked = role_write(http, session, user["id"], command(2), revoke=True, key=revoke_key)
        replay = role_write(http, session, user["id"], command(2), revoke=True, key=revoke_key)
        assert (
            revoked.status_code == replay.status_code == 200 and revoked.content == replay.content
        )
        absent = role_write(http, session, user["id"], command(3), revoke=True)
        assert absent.status_code == 404
        assert (
            scalar(
                database,
                "SELECT COUNT(*) FROM audit_events "
                "WHERE tenant_id=:tenant AND action LIKE 'role.%'",
                tenant,
            )
            == 2
        )


@pytest.mark.parametrize(
    "role,scope,has_lab",
    [
        ("safety_admin", "laboratory", True),
        ("rule_expert", "laboratory", True),
        ("inspector", "tenant", False),
        ("lab_manager", "tenant", False),
        ("remediator", "tenant", False),
        ("viewer", "tenant", False),
        ("viewer", "laboratory", False),
        ("safety_admin", "tenant", True),
    ],
)
def test_illegal_role_scope_is_rejected_atomically(identity, database, role, scope, has_lab):
    tenant, _, app = identity
    lab = lab_fixture(database, tenant) if has_lab else None
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        user = create_user(http, session)
        for revoke in (False, True):
            body = {**command(role=role, lab=lab), "scope_kind": scope}
            result = role_write(http, session, user["id"], body, revoke=revoke)
            assert result.status_code == 422, result.text
        assert http.get(f"/api/v1/users/{user['id']}").json()["data"]["version"] == 1
        assert (
            scalar(database, "SELECT COUNT(*) FROM api_idempotency WHERE tenant_id=:tenant", tenant)
            == 1
        )


def test_role_tenant_visibility_and_non_admin_access(identity, database):
    tenant, _, app = identity
    foreign = create_tenant(database, "roleforeign")
    foreign_lab = lab_fixture(database, foreign)
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        user = create_user(http, session)
        assert http.get(f"/api/v1/users/{foreign['admin_id']}/roles").status_code == 404
        assert (
            http.get(f"/api/v1/users/{user['id']}/roles?laboratory_id={foreign_lab}").status_code
            == 404
        )
        for revoke in (False, True):
            assert (
                role_write(http, session, foreign["admin_id"], command(), revoke=revoke).status_code
                == 404
            )
            assert (
                role_write(
                    http,
                    session,
                    user["id"],
                    command(role="viewer", lab=foreign_lab),
                    revoke=revoke,
                ).status_code
                == 404
            )
        basic = login(http, tenant, username="target").json()
        assert http.get(f"/api/v1/users/{user['id']}/roles").status_code == 403
        for revoke in (False, True):
            assert role_write(http, basic, user["id"], command(), revoke=revoke).status_code == 403


def test_role_pagination_filter_and_exact_scope_revoke(identity, database):
    tenant, _, app = identity
    labs = [lab_fixture(database, tenant) for _ in range(2)]
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        user = create_user(http, session)
        for version, lab in enumerate(labs, 1):
            assert (
                role_write(http, session, user["id"], command(version, "viewer", lab)).status_code
                == 200
            )
        assert role_write(http, session, user["id"], command(3)).status_code == 200
        path = f"/api/v1/users/{user['id']}/roles"
        full = http.get(path).json()["data"]
        assert full["total"] == 3
        assert full["items"] == sorted(
            full["items"], key=lambda x: (x["created_at"], x["id"]), reverse=True
        )
        pages = [
            http.get(path + f"?page={page}&page_size=1").json()["data"] for page in range(1, 5)
        ]
        assert [p["items"][0] for p in pages[:3]] == full["items"] and pages[3]["items"] == []
        assert all(p["total"] == 3 for p in pages)
        filtered = http.get(path + f"?laboratory_id={labs[0]}")
        assert_schema(filtered, "RoleAssignmentPage")
        assert filtered.json()["data"]["total"] == 1
        assert (
            role_write(
                http, session, user["id"], command(4, "viewer", labs[0]), revoke=True
            ).status_code
            == 200
        )
        remaining = http.get(path).json()["data"]["items"]
        assert {(a["role"], a["laboratory_id"]) for a in remaining} == {
            ("viewer", labs[1]),
            ("rule_expert", None),
        }


def test_disable_and_archive_deny_grants_but_allow_security_cleanup(identity, database):
    tenant, _, app = identity
    lab = lab_fixture(database, tenant)
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        user = create_user(http, session)
        assert role_write(http, session, user["id"], command(1, "viewer", lab)).status_code == 200
        disabled = http.post(
            f"/api/v1/users/{user['id']}/disable",
            json={"expected_version": 2, "reason": "test"},
            headers=headers(session),
        )
        assert disabled.status_code == 200
        assert role_write(http, session, user["id"], command(3)).status_code == 409
        with database.begin() as connection:
            connection.execute(
                text(
                    "UPDATE laboratories SET status='archived' WHERE tenant_id=:tenant AND id=:lab"
                ),
                {"tenant": tenant["tenant_id"], "lab": lab},
            )
        other = create_user(http, session, "other")
        assert role_write(http, session, other["id"], command(1, "viewer", lab)).status_code == 409
        revoked = role_write(http, session, user["id"], command(3, "viewer", lab), revoke=True)
        assert revoked.status_code == 200 and revoked.json()["data"]["status"] == "disabled"


@pytest.mark.parametrize("revoke", [False, True])
def test_role_audit_failure_rolls_back_role_version_epoch_sessions_and_idempotency(
    identity, database, revoke
):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http, TestClient(app, base_url=ORIGIN) as target:
        session = login(http, tenant).json()
        user = create_user(http, session)
        version = 1
        if revoke:
            assert role_write(http, session, user["id"], command()).status_code == 200
            version = 2
        assert login(target, tenant, username="target").status_code == 200
        before = scalar(
            database,
            "SELECT session_epoch FROM users WHERE tenant_id=:tenant AND id=:user",
            tenant,
            user=user["id"],
        )
        with patch(
            "packages.persistence.roles.append_audit",
            side_effect=RuntimeError("private audit failure"),
        ):
            result = role_write(http, session, user["id"], command(version), revoke=revoke)
        assert result.status_code == 500 and "private" not in result.text
        assert target.get("/api/v1/me").status_code == 200
        assert http.get(f"/api/v1/users/{user['id']}").json()["data"]["version"] == version
        assert (
            scalar(
                database,
                "SELECT session_epoch FROM users WHERE tenant_id=:tenant AND id=:user",
                tenant,
                user=user["id"],
            )
            == before
        )
        assert (
            scalar(database, "SELECT COUNT(*) FROM api_idempotency WHERE tenant_id=:tenant", tenant)
            == version
        )
        assert http.get(f"/api/v1/users/{user['id']}/roles").json()["data"]["total"] == int(revoke)


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_role_grants_commit_once(identity, database, same_key):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        user = create_user(http, session)
        cookie = http.cookies.get("labsafe_session")
    count = 10 if same_key else 2
    gate, key = Barrier(count), str(uuid4())

    def grant(_):
        with TestClient(app, base_url=ORIGIN) as http:
            gate.wait(timeout=10)
            return http.post(
                f"/api/v1/users/{user['id']}/roles",
                json=command(),
                headers={
                    **headers(session, key if same_key else None),
                    "Cookie": f"labsafe_session={cookie}",
                },
            )

    with ThreadPoolExecutor(max_workers=count) as pool:
        replies = list(pool.map(grant, range(count)))
    successful = [r for r in replies if r.status_code == 200]
    assert successful and len({r.content for r in successful}) == 1, [r.text for r in replies]
    if same_key:
        assert all(r.status_code in {200, 409} for r in replies)
    else:
        assert sorted(r.status_code for r in replies) == [200, 409]
        assert (
            next(r for r in replies if r.status_code == 409).json()["error"]["code"]
            == "VERSION_CONFLICT"
        )
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM user_roles WHERE tenant_id=:tenant AND user_id=:user",
            tenant,
            user=user["id"],
        )
        == 1
    )
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM audit_events WHERE tenant_id=:tenant AND action='role.grant'",
            tenant,
        )
        == 1
    )
    assert (
        scalar(database, "SELECT COUNT(*) FROM api_idempotency WHERE tenant_id=:tenant", tenant)
        == 2
    )


@pytest.mark.parametrize("self_revoke", [True, False])
def test_two_admin_role_revocation_race_keeps_one_admin(identity, database, self_revoke):
    tenant, service, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        first = login(http, tenant).json()
        first_token = http.cookies.get("labsafe_session")
        second_user = create_user(http, first, "admin2")
        assert (
            role_write(http, first, second_user["id"], command(role="safety_admin")).status_code
            == 200
        )
        second = login(http, tenant, username="admin2").json()
        second_token = http.cookies.get("labsafe_session")
    actors = [
        (tenant["admin_id"], 1, first, first_token),
        (second_user["id"], 2, second, second_token),
    ]
    gate = Barrier(2)
    original_limit = service.limits.user

    def synchronize(principal, *, write):
        original_limit(principal, write=write)
        if write:
            gate.wait(timeout=10)

    def revoke(index):
        _, _, session, token = actors[index]
        user, version, _, _ = actors[index if self_revoke else 1 - index]
        with TestClient(app, base_url=ORIGIN) as http:
            return http.post(
                f"/api/v1/users/{user}/revoke-role",
                json=command(version, "safety_admin"),
                headers={**headers(session), "Cookie": f"labsafe_session={token}"},
            )

    with (
        patch.object(service.limits, "user", side_effect=synchronize),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        replies = list(pool.map(revoke, range(2)))
    assert sorted(r.status_code for r in replies) == ([200, 409] if self_revoke else [200, 401]), [
        r.text for r in replies
    ]
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM user_roles ur JOIN users u ON u.id=ur.user_id "
            "AND u.tenant_id=ur.tenant_id JOIN roles r ON r.id=ur.role_id "
            "WHERE ur.tenant_id=:tenant AND u.status='active' AND r.code='safety_admin'",
            tenant,
        )
        == 1
    )


def test_self_grant_invalidates_old_session_and_blocks_cached_replay(identity):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        key = str(uuid4())
        response = role_write(http, session, tenant["admin_id"], command(), key=key)
        assert response.status_code == 200
        assert role_write(http, session, tenant["admin_id"], command(), key=key).status_code == 401
        renewed = login(http, tenant).json()
        replay = role_write(http, renewed, tenant["admin_id"], command(), key=key)
        assert replay.status_code == 200 and replay.content == response.content


def test_role_capacity_rejects_grant_without_invalidating_existing_session(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http, TestClient(app, base_url=ORIGIN) as target:
        session = login(http, tenant).json()
        user = create_user(http, session)
        labs = [lab_fixture(database, tenant) for _ in range(51)]
        # Fifty inspector assignments project to exactly 200 distinct action/lab pairs.
        with database.begin() as connection:
            role = connection.scalar(text("SELECT id FROM roles WHERE code='inspector'"))
            connection.execute(
                text(
                    "INSERT INTO user_roles "
                    "(id,tenant_id,user_id,role_id,scope_kind,laboratory_id) "
                    "VALUES (:id,:tenant,:user,:role,'laboratory',:lab)"
                ),
                [
                    {
                        "id": str(uuid4()),
                        "tenant": tenant["tenant_id"],
                        "user": user["id"],
                        "role": role,
                        "lab": lab,
                    }
                    for lab in labs[:50]
                ],
            )
        assert len(login(target, tenant, username="target").json()["data"]["permissions"]) == 200
        rejected = role_write(http, session, user["id"], command(1, "inspector", labs[-1]))
        assert rejected.status_code == 409, rejected.text
        assert target.get("/api/v1/me").status_code == 200
        assert http.get(f"/api/v1/users/{user['id']}/roles").json()["data"]["total"] == 50
        assert http.get(f"/api/v1/users/{user['id']}").json()["data"]["version"] == 1
