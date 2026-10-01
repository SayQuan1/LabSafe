"""Foundation APIs against disposable MySQL/Redis, without seeded business success."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from sqlalchemy import event, text

from packages.persistence.security import load_principal
from tests.persistence.test_identity_mysql import ORIGIN, assert_schema, headers, login
from tests.persistence.test_roles_mysql import command, create_user, role_write, scalar
from tests.persistence.test_security_mysql import create_tenant


def post(http, session, path, body, *, key=None):
    return http.post("/api/v1" + path, json=body, headers=headers(session, key))


def created(http, session, path, body, schema):
    response = post(http, session, path, body)
    assert response.status_code == 201, response.text
    assert_schema(response, schema)
    return response.json()["data"]


def template_body(count=2):
    return {
        "name": "Synthetic template",
        "items": [
            {
                "id": str(uuid4()),
                "code": f"item-{i}",
                "title": f"Item {i}",
                "capture_hint": "Synthetic capture hint",
                "sort_order": i,
                "required": i % 2 == 0,
            }
            for i in range(count)
        ],
    }


def organization(http, session):
    college = created(
        http, session, "/colleges", {"code": str(uuid4()), "name": "Synthetic college"}, "College"
    )
    lab = created(
        http,
        session,
        "/laboratories",
        {"college_id": college["id"], "code": str(uuid4()), "name": "Synthetic lab"},
        "Laboratory",
    )
    room = created(
        http,
        session,
        f"/laboratories/{lab['id']}/locations",
        {"parent_id": None, "type": "room", "label": "Room"},
        "Location",
    )
    return college, lab, room


def published(http, session, count=2):
    template = created(http, session, "/templates", template_body(count), "Template")
    response = post(
        http,
        session,
        f"/templates/{template['id']}/publish",
        {"expected_version": 1, "reason": "Ready for synthetic test"},
    )
    assert response.status_code == 200, response.text
    assert_schema(response, "Template")
    return response.json()["data"]


def inspection_body(lab, template, *locations):
    return {
        "laboratory_id": lab["id"],
        "template_id": template["id"],
        "location_ids": [location["id"] for location in locations],
    }


def count_rows(database, tenant, table):
    # Table names below are fixed test literals, not user input.
    return scalar(database, f"SELECT COUNT(*) FROM {table} WHERE tenant_id=:tenant", tenant)


def test_complete_foundation_chain_and_all_response_schemas(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        college, lab, room = organization(http, session)
        area = created(
            http,
            session,
            f"/laboratories/{lab['id']}/locations",
            {"parent_id": room["id"], "type": "area", "label": "Area"},
            "Location",
        )
        template = published(http, session)
        result = created(
            http, session, "/inspections", inspection_body(lab, template, room, area), "Inspection"
        )
        assert result["status"] == "draft" and len(set(result["item_ids"])) == 4
        assert result["inspector_id"] == tenant["admin_id"]
        assert template["status"] == "published" and template["version"] == 2
        for path, resource, schema in [
            ("colleges", college, "College"),
            ("laboratories", lab, "Laboratory"),
            ("templates", template, "Template"),
            ("inspections", result, "Inspection"),
        ]:
            assert_schema(http.get(f"/api/v1/{path}/{resource['id']}"), schema)
            page = http.get(f"/api/v1/{path}?page_size=1")
            assert_schema(page, schema + "Page")
            assert page.json()["data"]["total"] == 1
            assert "tenant_id" not in page.json()["data"]["items"][0]
        assert_schema(http.get(f"/api/v1/laboratories/{lab['id']}/locations"), "LocationPage")
    with database.begin() as connection:
        rows = (
            connection.execute(
                text("SELECT * FROM inspection_items WHERE inspection_id=:id"), {"id": result["id"]}
            )
            .mappings()
            .all()
        )
        assert {(r["template_item_id"], r["location_id"]) for r in rows} == {
            (i["id"], loc["id"]) for i in template["items"] for loc in (room, area)
        }
        for row in rows:
            assert row["status"] == "draft" and row["submission_revision"] == 0
            assert row["current_run_id"] is row["current_fact_revision_id"] is None
            assert row["current_evaluation_id"] is row["review_outcome"] is None


def test_template_publish_clone_versions_and_replay(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        body, key = template_body(), str(uuid4())
        original = post(http, session, "/templates", body, key=key)
        assert original.status_code == 201
        assert post(http, session, "/templates", body, key=key).content == original.content
        assert (
            post(http, session, "/templates", {**body, "name": "Other"}, key=key).status_code == 409
        )
        source = original.json()["data"]
        path = f"/templates/{source['id']}/publish"
        publish_key = str(uuid4())
        version = {"expected_version": 1, "reason": "Publish reason"}
        result = post(http, session, path, version, key=publish_key)
        assert result.status_code == 200
        assert post(http, session, path, version, key=publish_key).content == result.content
        assert post(http, session, path, version).json()["error"]["code"] == "VERSION_CONFLICT"
        assert post(http, session, path, {**version, "expected_version": 2}).status_code == 409
        clone_path, clone_key = f"/templates/{source['id']}/clone", str(uuid4())
        reason = {"expected_version": 2, "reason": "Clone audit reason"}
        first = post(http, session, clone_path, reason, key=clone_key)
        assert first.status_code == 201, first.text
        assert_schema(first, "Template")
        assert post(http, session, clone_path, reason, key=clone_key).content == first.content
        clone = first.json()["data"]
        assert (clone["revision"], clone["version"], clone["status"]) == (2, 1, "draft")
        assert not ({i["id"] for i in source["items"]} & {i["id"] for i in clone["items"]})
        assert [{k: v for k, v in i.items() if k != "id"} for i in source["items"]] == [
            {k: v for k, v in i.items() if k != "id"} for i in clone["items"]
        ]
        second = post(
            http,
            session,
            f"/templates/{clone['id']}/clone",
            {"expected_version": 1, "reason": "Clone later revision"},
        )
        assert second.json()["data"]["revision"] == 3
        current = http.get(f"/api/v1/templates/{source['id']}").json()["data"]
        assert current == result.json()["data"]
    assert (
        scalar(
            database,
            "SELECT reason FROM audit_events WHERE tenant_id=:tenant "
            "AND action='template.clone' AND resource_id=:id",
            tenant,
            id=clone["id"],
        )
        == reason["reason"]
    )
    assert count_rows(database, tenant, "inspection_templates") == 3


@pytest.mark.parametrize("field", ["id", "code", "sort_order", "normalized_code"])
def test_duplicate_template_fields_roll_back(identity, database, field):
    tenant, _, app = identity
    body = template_body()
    if field == "normalized_code":
        body["items"][1]["code"] = " " + body["items"][0]["code"] + " "
    else:
        body["items"][1][field] = body["items"][0][field]
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        assert post(http, session, "/templates", body).status_code == 422
    assert count_rows(database, tenant, "inspection_templates") == 0
    assert count_rows(database, tenant, "template_items") == 0
    assert count_rows(database, tenant, "api_idempotency") == 0


def test_duplicate_database_codes_and_child_ids_are_409(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        college, lab, _ = organization(http, session)
        for path, body in [
            ("/colleges", {"name": "Other", "code": college["code"]}),
            ("/laboratories", {"name": "Other", "code": lab["code"], "college_id": college["id"]}),
        ]:
            result = post(http, session, path, body)
            assert result.status_code == 409 and result.json()["error"]["code"] == "STATE_CONFLICT"
        body = template_body()
        created(http, session, "/templates", body, "Template")
        assert post(http, session, "/templates", body).status_code == 409
    assert count_rows(database, tenant, "inspection_templates") == 1
    assert count_rows(database, tenant, "api_idempotency") == 4


@pytest.mark.parametrize("role", ["inspector", "viewer", "rule_expert", "none"])
def test_scoped_visibility_public_projections_and_capture(identity, database, role):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as reader:
        session = login(admin, tenant).json()
        college, lab, room = organization(admin, session)
        _, other_lab, other_room = organization(admin, session)
        template = published(admin, session)
        draft = created(admin, session, "/templates", template_body(), "Template")
        inspection = created(
            admin, session, "/inspections", inspection_body(lab, template, room), "Inspection"
        )
        other = created(
            admin,
            session,
            "/inspections",
            inspection_body(other_lab, template, other_room),
            "Inspection",
        )
        user = create_user(admin, session)
        if role != "none":
            assert (
                role_write(
                    admin,
                    session,
                    user["id"],
                    command(role=role, lab=None if role == "rule_expert" else lab["id"]),
                ).status_code
                == 200
            )
        actor = login(reader, tenant, username="target").json()
        public_status = 403 if role == "none" else 200
        assert reader.get(f"/api/v1/colleges/{college['id']}").status_code == public_status
        assert reader.get(f"/api/v1/templates/{template['id']}").status_code == public_status
        assert reader.get(f"/api/v1/templates/{draft['id']}").status_code == (
            403 if role == "none" else 404
        )
        if role != "none":
            assert reader.get("/api/v1/templates").json()["data"]["total"] == 1
        for path in ("/laboratories", "/inspections"):
            result = reader.get("/api/v1" + path + "?page_size=1")
            assert result.status_code == (200 if role in {"inspector", "viewer"} else 403)
            if result.status_code == 200:
                assert result.json()["data"]["total"] == 1
                assert (
                    result.json()["data"]["items"][0]["id"]
                    == (lab if path == "/laboratories" else inspection)["id"]
                )
            assert (
                reader.get("/api/v1" + path + f"?laboratory_id={other_lab['id']}").status_code
                == 404
            )
        assert reader.get(f"/api/v1/inspections/{other['id']}").status_code == 404
        assert reader.get(f"/api/v1/laboratories/{other_lab['id']}/locations").status_code == 404
        result = post(reader, actor, "/inspections", inspection_body(lab, template, room))
        assert result.status_code == (
            201 if role == "inspector" else 403 if role == "viewer" else 404
        )
        assert (
            post(reader, actor, "/colleges", {"name": "Forbidden", "code": "f"}).status_code == 403
        )
        assert post(reader, actor, "/templates", template_body()).status_code == 403


def test_cross_tenant_resources_are_hidden(identity, database):
    tenant, _, app = identity
    foreign = create_tenant(database, "foundationforeign")
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, foreign).json()
        college, lab, room = organization(http, session)
        template = published(http, session)
        inspection = created(
            http, session, "/inspections", inspection_body(lab, template, room), "Inspection"
        )
        session = login(http, tenant).json()
        for kind, obj in [
            ("colleges", college),
            ("laboratories", lab),
            ("templates", template),
            ("inspections", inspection),
        ]:
            assert http.get(f"/api/v1/{kind}/{obj['id']}").status_code == 404
            assert http.get(f"/api/v1/{kind}").json()["data"]["total"] == 0
        assert (
            post(
                http,
                session,
                "/laboratories",
                {"college_id": college["id"], "name": "Foreign", "code": "foreign"},
            ).status_code
            == 404
        )
        assert (
            post(
                http,
                session,
                f"/laboratories/{lab['id']}/locations",
                {"parent_id": None, "type": "room", "label": "Foreign"},
            ).status_code
            == 404
        )
        assert (
            post(
                http,
                session,
                f"/templates/{template['id']}/clone",
                {"expected_version": 2, "reason": "Forbidden"},
            ).status_code
            == 404
        )
        assert (
            post(http, session, "/inspections", inspection_body(lab, template, room)).status_code
            == 404
        )


def test_location_hierarchy_and_archived_parents(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        college, lab, room = organization(http, session)
        _, _, other_room = organization(http, session)
        path = f"/laboratories/{lab['id']}/locations"
        for kind, parent in [
            ("room", room["id"]),
            ("area", None),
            ("shelf", None),
            ("cabinet", None),
        ]:
            assert (
                post(
                    http, session, path, {"parent_id": parent, "type": kind, "label": "Bad"}
                ).status_code
                == 422
            )
        assert (
            post(
                http, session, path, {"parent_id": other_room["id"], "type": "area", "label": "Bad"}
            ).status_code
            == 404
        )
        area = created(
            http, session, path, {"parent_id": room["id"], "type": "area", "label": "A"}, "Location"
        )
        for kind in ("shelf", "cabinet"):
            for parent in (room, area):
                created(
                    http,
                    session,
                    path,
                    {"parent_id": parent["id"], "type": kind, "label": kind},
                    "Location",
                )
        with database.begin() as connection:
            connection.execute(
                text("UPDATE locations SET status='archived' WHERE id=:id"), {"id": room["id"]}
            )
            connection.execute(
                text("UPDATE colleges SET status='archived' WHERE id=:id"), {"id": college["id"]}
            )
        assert (
            post(
                http, session, path, {"parent_id": room["id"], "type": "area", "label": "Bad"}
            ).status_code
            == 409
        )
        assert (
            post(
                http,
                session,
                "/laboratories",
                {"college_id": college["id"], "code": "bad", "name": "Bad"},
            ).status_code
            == 409
        )
        with database.begin() as connection:
            connection.execute(
                text("UPDATE laboratories SET status='archived' WHERE id=:id"), {"id": lab["id"]}
            )
        assert (
            post(
                http, session, path, {"parent_id": None, "type": "room", "label": "Bad"}
            ).status_code
            == 409
        )


@pytest.mark.parametrize(
    "failure",
    ["draft", "foreign_location", "archived_location", "archived_lab", "duplicate_location"],
)
def test_inspection_preconditions_leave_no_partial_writes(identity, database, failure):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        _, lab, room = organization(http, session)
        template = (
            created(http, session, "/templates", template_body(), "Template")
            if failure == "draft"
            else published(http, session)
        )
        if failure == "foreign_location":
            _, _, room = organization(http, session)
        if failure.startswith("archived"):
            table, obj = (
                ("locations", room) if failure == "archived_location" else ("laboratories", lab)
            )
            with database.begin() as connection:
                connection.execute(
                    text(f"UPDATE {table} SET status='archived' WHERE id=:id"), {"id": obj["id"]}
                )
        before = count_rows(database, tenant, "api_idempotency")
        body = inspection_body(lab, template, room)
        if failure == "duplicate_location":
            body["location_ids"] *= 2
        response = post(http, session, "/inspections", body)
        assert response.status_code == (
            404
            if failure == "foreign_location"
            else 422
            if failure == "duplicate_location"
            else 409
        ), response.text
    assert count_rows(database, tenant, "inspections") == 0
    assert count_rows(database, tenant, "inspection_items") == 0
    assert count_rows(database, tenant, "api_idempotency") == before


def test_inspection_100_boundary_and_oversize_are_atomic(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        _, lab, room = organization(http, session)
        other_room = created(
            http,
            session,
            f"/laboratories/{lab['id']}/locations",
            {"parent_id": None, "type": "room", "label": "Second"},
            "Location",
        )
        template = published(http, session, 100)
        accepted = created(
            http, session, "/inspections", inspection_body(lab, template, room), "Inspection"
        )
        assert len(accepted["item_ids"]) == 100
        assert (
            post(
                http, session, "/inspections", inspection_body(lab, template, room, other_room)
            ).status_code
            == 422
        )
        assert post(http, session, "/templates", template_body(101)).status_code == 422
    assert count_rows(database, tenant, "inspections") == 1
    assert count_rows(database, tenant, "inspection_items") == 100


@pytest.mark.parametrize("stage", ["template_audit", "inspection_audit", "second_child"])
def test_atomic_rollback_including_children_audit_and_idempotency(identity, database, stage):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        _, lab, room = organization(http, session)
        template = published(http, session)
        counts = {
            table: count_rows(database, tenant, table)
            for table in (
                "inspection_templates",
                "template_items",
                "inspections",
                "inspection_items",
                "audit_events",
                "api_idempotency",
            )
        }
        if stage == "second_child":
            # First child is inserted before the second hits an existing global child PK.
            body = template_body()
            body["items"][1]["id"] = template["items"][0]["id"]
            response = post(http, session, "/templates", body)
            assert response.status_code == 409
        else:
            module = "templates" if stage == "template_audit" else "inspections"
            path, body = (
                ("/templates", template_body())
                if stage == "template_audit"
                else ("/inspections", inspection_body(lab, template, room))
            )
            with patch(
                f"packages.persistence.{module}.append_audit",
                side_effect=RuntimeError("private failure"),
            ):
                response = post(http, session, path, body)
            assert response.status_code == 500 and "private failure" not in response.text
        assert {table: count_rows(database, tenant, table) for table in counts} == counts


@pytest.mark.parametrize("kind", ["college", "template", "inspection"])
def test_ten_concurrent_same_key_creates_once(identity, database, kind):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        if kind == "inspection":
            _, lab, room = organization(http, session)
            template = published(http, session)
            body = inspection_body(lab, template, room)
        else:
            body = (
                {"name": "Concurrent", "code": "concurrent"}
                if kind == "college"
                else template_body()
            )
        cookie = http.cookies.get("labsafe_session")
    gate, key = Barrier(10), str(uuid4())
    path = {"college": "/colleges", "template": "/templates", "inspection": "/inspections"}[kind]

    def invoke(_):
        with TestClient(app, base_url=ORIGIN) as http:
            gate.wait(timeout=15)
            return http.post(
                "/api/v1" + path,
                json=body,
                headers={**headers(session, key), "Cookie": f"labsafe_session={cookie}"},
            )

    with ThreadPoolExecutor(max_workers=10) as pool:
        replies = list(pool.map(invoke, range(10)))
    successes = [r for r in replies if r.status_code == 201]
    assert successes and len({r.content for r in successes}) == 1, [r.text for r in replies]
    assert all(r.status_code in {201, 409} for r in replies), [r.text for r in replies]
    for reply in replies:
        if reply.status_code == 409:
            assert reply.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM audit_events WHERE tenant_id=:tenant AND action=:action",
            tenant,
            action=kind + ".create",
        )
        == 1
    )


@pytest.mark.parametrize("mixed_sources", [False, True])
def test_concurrent_clone_allocates_distinct_revisions_across_sessions(
    identity, database, mixed_sources
):
    tenant, _, app = identity
    sessions = []
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        template = published(http, session)
        sources = [template] * 4
        if mixed_sources:
            child = post(
                http,
                session,
                f"/templates/{template['id']}/clone",
                {"expected_version": 2, "reason": "Create second clone source"},
            )
            assert child.status_code == 201
            sources = [template, child.json()["data"]] * 2
        for _ in range(4):
            session = login(http, tenant).json()
            sessions.append((session, http.cookies.get("labsafe_session")))
    gate = Barrier(4)

    def invoke(index):
        session, cookie = sessions[index]
        with TestClient(app, base_url=ORIGIN) as http:
            gate.wait(timeout=15)
            return http.post(
                f"/api/v1/templates/{sources[index]['id']}/clone",
                json={"expected_version": sources[index]["version"], "reason": "Concurrent clone"},
                headers={**headers(session), "Cookie": f"labsafe_session={cookie}"},
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(invoke, range(4)))
    assert all(r.status_code == 201 for r in replies), [r.text for r in replies]
    first = 3 if mixed_sources else 2
    assert sorted(r.json()["data"]["revision"] for r in replies) == list(range(first, first + 4))
    assert count_rows(database, tenant, "inspection_templates") == first + 3


def test_revoke_capture_blocks_successful_key_replay(identity):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as target:
        session = login(admin, tenant).json()
        _, lab, room = organization(admin, session)
        template = published(admin, session)
        user = create_user(admin, session)
        assert (
            role_write(
                admin, session, user["id"], command(role="inspector", lab=lab["id"])
            ).status_code
            == 200
        )
        actor, key = login(target, tenant, username="target").json(), str(uuid4())
        body = inspection_body(lab, template, room)
        original = post(target, actor, "/inspections", body, key=key)
        assert original.status_code == 201
        assert post(target, actor, "/inspections", body, key=key).content == original.content
        assert (
            role_write(
                admin, session, user["id"], command(2, "inspector", lab["id"]), revoke=True
            ).status_code
            == 200
        )
        assert post(target, actor, "/inspections", body, key=key).status_code == 401
        actor = login(target, tenant, username="target").json()
        assert post(target, actor, "/inspections", body, key=key).status_code == 404


def test_redis_failure_prevents_foundation_write(identity, database):
    tenant, service, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        with patch.object(service.limits.redis, "eval", side_effect=ConnectionError("offline")):
            assert post(http, session, "/colleges", {"name": "N", "code": "C"}).status_code == 503
    assert count_rows(database, tenant, "colleges") == 0
    assert count_rows(database, tenant, "api_idempotency") == 0


def test_concurrent_authentication_keeps_user_lock_shared(identity):
    tenant, service, app = identity
    tokens = []
    with TestClient(app, base_url=ORIGIN) as http:
        for _ in range(4):
            assert login(http, tenant).status_code == 200
            tokens.append(http.cookies.get("labsafe_session"))
    gate = Barrier(4)

    def synchronized_principal(*args):
        principal = load_principal(*args)
        gate.wait(timeout=15)  # All four transactions now hold users S concurrently.
        return principal

    def authenticate(token):
        with service.transaction() as connection:
            return service.sessions.authenticate(connection, token).principal.user_id

    with patch("packages.persistence.security.load_principal", side_effect=synchronized_principal):
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(authenticate, tokens)) == [tenant["admin_id"]] * 4


def test_replay_does_not_rerun_template_publication_precondition(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as target:
        session = login(admin, tenant).json()
        _, lab, room = organization(admin, session)
        template = published(admin, session)
        user = create_user(admin, session)
        assert (
            role_write(
                admin, session, user["id"], command(role="inspector", lab=lab["id"])
            ).status_code
            == 200
        )
        actor, key = login(target, tenant, username="target").json(), str(uuid4())
        body = inspection_body(lab, template, room)
        original = post(target, actor, "/inspections", body, key=key)
        assert original.status_code == 201
        # Simulate a future retirement command; this batch has no retirement API.
        with database.begin() as connection:
            connection.execute(
                text("UPDATE inspection_templates SET status='retired' WHERE id=:id"),
                {"id": template["id"]},
            )
        assert post(target, actor, "/inspections", body, key=key).content == original.content
        assert post(target, actor, "/inspections", body).status_code == 404


def test_pagination_snapshot_and_tie_order(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        colleges = [
            created(http, session, "/colleges", {"name": "College", "code": str(i)}, "College")
            for i in range(3)
        ]
        with database.begin() as connection:
            connection.execute(
                text(
                    "UPDATE colleges SET created_at='2026-01-01 00:00:00' WHERE tenant_id=:tenant"
                ),
                {"tenant": tenant["tenant_id"]},
            )
        page = http.get("/api/v1/colleges?page=2&page_size=1").json()["data"]
        assert page["total"] == 3
        assert page["items"][0]["id"] == sorted(c["id"] for c in colleges)[1]
        inserted = False

        def insert_after_count(connection, cursor, statement, parameters, context, executemany):
            nonlocal inserted
            if not inserted and statement.startswith("SELECT COUNT(*) FROM colleges WHERE"):
                inserted = True
                with database.begin() as other:
                    other.execute(
                        text(
                            "INSERT INTO colleges (id,tenant_id,code,name) "
                            "VALUES (:id,:tenant,'concurrent','Concurrent fixture')"
                        ),
                        {"id": str(uuid4()), "tenant": tenant["tenant_id"]},
                    )

        event.listen(database, "after_cursor_execute", insert_after_count)
        try:
            response = http.get("/api/v1/colleges")
            assert response.status_code == 200, response.text
            assert response.json()["data"]["total"] == len(response.json()["data"]["items"]) == 3
            assert inserted
        finally:
            event.remove(database, "after_cursor_execute", insert_after_count)
        assert http.get("/api/v1/colleges").json()["data"]["total"] == 4


def test_archived_college_is_not_a_public_projection(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as target:
        session = login(admin, tenant).json()
        college, lab, _ = organization(admin, session)
        draft = created(admin, session, "/templates", template_body(), "Template")
        user = create_user(admin, session)
        assert (
            role_write(
                admin, session, user["id"], command(role="viewer", lab=lab["id"])
            ).status_code
            == 200
        )
        assert login(target, tenant, username="target").status_code == 200
        with database.begin() as connection:
            connection.execute(
                text("UPDATE colleges SET status='archived' WHERE id=:id"), {"id": college["id"]}
            )
        assert target.get("/api/v1/colleges").json()["data"]["total"] == 0
        assert target.get(f"/api/v1/colleges/{college['id']}").status_code == 404
        assert admin.get(f"/api/v1/colleges/{college['id']}").status_code == 200
        assert target.get(f"/api/v1/templates/{draft['id']}").status_code == 404


def test_preflight_is_revalidated_before_foundation_commit(identity, database):
    tenant, service, app = identity
    active = 0
    transaction = service.transaction

    @contextmanager
    def tracked_transaction():
        nonlocal active
        with transaction() as connection:
            active += 1
            try:
                yield connection
            finally:
                active -= 1

    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        original_limit = service.limits.user

        def revoke_after_preflight(principal, *, write):
            original_limit(principal, write=write)
            assert active == 0
            with database.begin() as connection:
                connection.execute(
                    text("UPDATE sessions SET revoked_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant"),
                    {"tenant": tenant["tenant_id"]},
                )

        with (
            patch.object(service, "transaction", side_effect=tracked_transaction),
            patch.object(service.limits, "user", side_effect=revoke_after_preflight),
        ):
            result = post(http, session, "/colleges", {"code": "denied", "name": "Denied"})
        assert result.status_code == 401, result.text
    assert count_rows(database, tenant, "api_idempotency") == 0
    assert count_rows(database, tenant, "colleges") == 0
