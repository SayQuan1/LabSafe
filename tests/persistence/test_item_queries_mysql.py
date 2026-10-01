"""Read-only item APIs on disposable MySQL/Redis; synthetic history is labeled."""

from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError
from sqlalchemy import event, text

from packages.domain.workflow import ItemStatus
from packages.persistence.inspection_items import PUBLIC_FIELDS
from tests.persistence.factories import insert
from tests.persistence.test_foundations_mysql import (
    count_rows,
    created,
    inspection_body,
    organization,
    published,
)
from tests.persistence.test_identity_mysql import ORIGIN, assert_schema, login
from tests.persistence.test_roles_mysql import command, create_user, role_write
from tests.persistence.test_security_mysql import create_tenant


def inspection(http, session, *, count=2):
    _, lab, room = organization(http, session)
    template = published(http, session, count)
    result = created(
        http, session, "/inspections", inspection_body(lab, template, room), "Inspection"
    )
    return result, lab, room, template


def paths(value):
    return (
        f"/api/v1/inspections/{value['id']}/items",
        f"/api/v1/inspection-items/{value['item_ids'][0]}",
    )


def business_snapshot(database, tenant):
    tables = (
        "inspections",
        "inspection_items",
        "audit_events",
        "inference_runs",
        "outbox_events",
        "api_idempotency",
        "task_runs",
        "fact_revisions",
        "rule_evaluations",
        "review_actions",
        "findings",
    )
    with database.begin() as connection:
        return {
            table: [
                dict(row)
                for row in connection.execute(
                    text(f"SELECT * FROM {table} WHERE tenant_id=:tenant ORDER BY id"),
                    {"tenant": tenant["tenant_id"]},
                ).mappings()
            ]
            for table in tables
        }


def test_real_creation_list_detail_contracts_and_no_business_side_effects(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, lab, room, _ = inspection(http, session)
        before = business_snapshot(database, tenant)
        listed = http.get(paths(value)[0])
        assert listed.status_code == 200, listed.text
        assert_schema(listed, "InspectionItemPage")
        page = listed.json()["data"]
        assert (page["total"], page["page"], page["page_size"]) == (2, 1, 20)
        assert {i["id"] for i in page["items"]} == set(value["item_ids"])
        for row in page["items"]:
            response = http.get(f"/api/v1/inspection-items/{row['id']}")
            assert response.status_code == 200, response.text
            assert_schema(response, "InspectionItem")
            assert response.json()["data"] == row
            assert set(row) == set(PUBLIC_FIELDS) | {"allowed_actions"}
            assert row["inspection_id"] == value["id"]
            assert row["laboratory_id"] == lab["id"] and row["location_id"] == room["id"]
            assert row["status"] == "draft" and row["submission_revision"] == 0
            assert row["current_run_id"] is row["current_fact_revision_id"] is None
            assert row["review_outcome"] is None and row["allowed_actions"] == []
            assert row["created_at"].endswith("Z") and row["updated_at"].endswith("Z")
            assert response.headers["cache-control"] == "no-store"
        assert business_snapshot(database, tenant) == before


@pytest.mark.parametrize(
    "role",
    ["safety_admin", "inspector", "viewer", "remediator", "lab_manager", "rule_expert", "none"],
)
def test_scope_is_checked_before_count_or_data_and_filters_do_not_expand_scope(
    identity, database, role
):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as reader:
        session = login(admin, tenant).json()
        value, lab, _, _ = inspection(admin, session)
        other, other_lab, _, _ = inspection(admin, session)
        user = create_user(admin, session)
        if role != "none":
            granted = role_write(
                admin,
                session,
                user["id"],
                command(
                    role=role, lab=None if role in {"safety_admin", "rule_expert"} else lab["id"]
                ),
            )
            assert granted.status_code == 200, granted.text
        assert login(reader, tenant, username="target").status_code == 200
        statements = []

        def observe(connection, cursor, statement, parameters, context, executemany):
            if "FROM inspection_items item" in statement:
                statements.append(statement)

        event.listen(database, "before_cursor_execute", observe)
        try:
            listed = reader.get(paths(value)[0])
            if role in {"rule_expert", "none"}:
                assert listed.status_code == 404
                assert statements == []  # Parent authority fails before count/page queries.
            else:
                assert listed.status_code == 200, listed.text
                assert listed.json()["data"]["total"] == 2
                assert len(listed.json()["data"]["items"]) == 2
        finally:
            event.remove(database, "before_cursor_execute", observe)
        allowed = role not in {"rule_expert", "none"}
        assert reader.get(paths(value)[1]).status_code == (200 if allowed else 404)
        for path in paths(other):
            assert reader.get(path).status_code == (200 if role == "safety_admin" else 404)
        filtered = reader.get(paths(value)[0] + f"?laboratory_id={other_lab['id']}")
        if role == "safety_admin":
            assert filtered.status_code == 200
            assert filtered.json()["data"]["items"] == [] and filtered.json()["data"]["total"] == 0
        else:
            assert filtered.status_code == 404
        matching = reader.get(paths(value)[0] + f"?laboratory_id={lab['id']}")
        assert matching.status_code == (200 if allowed else 404)
        if allowed:
            assert matching.json()["data"]["total"] == 2
        assert reader.get(paths(value)[0] + f"?laboratory_id={uuid4()}").status_code == 404


def test_missing_foreign_and_empty_parent_are_distinguished(identity, database):
    tenant, _, app = identity
    foreign = create_tenant(database, "itemsforeign")
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, foreign).json()
        other, _, _, _ = inspection(http, session)
        session = login(http, tenant).json()
        value, lab, _, template = inspection(http, session)
        for path in (
            *paths(other),
            f"/api/v1/inspections/{uuid4()}/items",
            f"/api/v1/inspection-items/{uuid4()}",
        ):
            assert http.get(path).status_code == 404
        # Legacy/DDL-only empty parent; the current create API always creates children.
        with database.begin() as connection:
            empty_id = insert(
                connection,
                "inspections",
                tenant_id=tenant["tenant_id"],
                laboratory_id=lab["id"],
                template_id=template["id"],
                inspector_id=tenant["admin_id"],
                status="draft",
            )
        result = http.get(f"/api/v1/inspections/{empty_id}/items")
        assert result.status_code == 200
        assert result.json()["data"] == {"items": [], "total": 0, "page": 1, "page_size": 20}
        assert http.get(paths(value)[0]).json()["data"]["total"] == 2


@pytest.mark.parametrize("status", list(ItemStatus))
def test_all_item_states_remain_readable_without_advertising_unimplemented_commands(
    identity, database, status
):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session)
        # Synthetic status snapshots test projection, not an implemented lifecycle.
        with database.begin() as connection:
            connection.execute(
                text("UPDATE inspection_items SET status=:status WHERE inspection_id=:id"),
                {"status": str(status), "id": value["id"]},
            )
        for path in paths(value):
            response = http.get(path)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            for row in data.get("items", [data]):
                assert row["status"] == status and row["allowed_actions"] == []


@pytest.mark.parametrize("parent_status", ["completed", "cancelled"])
def test_closed_parents_remain_readable_with_safe_actions(identity, database, parent_status):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session)
        with database.begin() as connection:
            connection.execute(
                text("UPDATE inspections SET status=:status WHERE id=:id"),
                {"status": parent_status, "id": value["id"]},
            )
        assert http.get(paths(value)[0]).json()["data"]["total"] == 2
        assert http.get(paths(value)[1]).json()["data"]["allowed_actions"] == []


def test_non_null_history_pointers_are_projected_without_exposing_internal_fields(
    identity, database
):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, lab, _, _ = inspection(http, session)
        target = value["item_ids"][0]
        with database.begin() as connection:

            def add(table, **values):
                return insert(connection, table, tenant_id=tenant["tenant_id"], **values)

            # Synthetic legal FK history, not model execution or a completed review command.
            dictionary = add("dictionary_versions")
            model = add("model_versions", dictionary_version_id=dictionary)
            rules = add("rule_bundles")
            run = add(
                "inference_runs",
                item_id=target,
                laboratory_id=lab["id"],
                model_bundle_id=model,
                dictionary_version_id=dictionary,
                rule_bundle_id=rules,
            )
            fact = add("fact_revisions", item_id=target, run_id=run)
            evaluation = add(
                "rule_evaluations",
                item_id=target,
                run_id=run,
                fact_revision_id=fact,
                rule_bundle_id=rules,
            )
            connection.execute(
                text(
                    "UPDATE inspection_items SET current_run_id=:run,"
                    "current_fact_revision_id=:fact,current_evaluation_id=:evaluation,"
                    "submission_revision=2,version=3,status='completed',review_outcome='no_issue' "
                    "WHERE id=:id"
                ),
                {"id": target, "run": run, "fact": fact, "evaluation": evaluation},
            )
        result = http.get(paths(value)[1])
        assert result.status_code == 200, result.text
        assert_schema(result, "InspectionItem")
        row = result.json()["data"]
        assert (row["current_run_id"], row["current_fact_revision_id"]) == (run, fact)
        assert (row["submission_revision"], row["version"], row["review_outcome"]) == (
            2,
            3,
            "no_issue",
        )
        assert set(row) == set(PUBLIC_FIELDS) | {"allowed_actions"}
        assert row["allowed_actions"] == []
        assert row in http.get(paths(value)[0]).json()["data"]["items"]


def test_full_capacity_paging_tie_order_and_empty_late_page(identity, database):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, _, _, _ = inspection(http, session, count=100)
        with database.begin() as connection:
            connection.execute(
                text(
                    "UPDATE inspection_items SET created_at='2026-01-01 00:00:00' "
                    "WHERE inspection_id=:id"
                ),
                {"id": value["id"]},
            )
        url = paths(value)[0]
        page = http.get(url + "?page_size=100")
        assert page.status_code == 200, page.text
        assert_schema(page, "InspectionItemPage")
        expected = sorted(value["item_ids"], reverse=True)
        assert [i["id"] for i in page.json()["data"]["items"]] == expected
        assert page.json()["data"]["total"] == 100
        middle = http.get(url + "?page=3&page_size=20").json()["data"]
        assert [i["id"] for i in middle["items"]] == expected[40:60]
        late = http.get(url + "?page=6&page_size=20").json()["data"]
        assert late == {"items": [], "total": 100, "page": 6, "page_size": 20}


@pytest.mark.parametrize("mutation", ["update", "insert"])
def test_count_and_item_projection_share_one_snapshot_during_concurrent_change(
    identity, database, mutation
):
    tenant, _, app = identity
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        value, lab, _, template = inspection(http, session)
        extra_room = created(
            http,
            session,
            f"/laboratories/{lab['id']}/locations",
            {"parent_id": None, "type": "room", "label": "Extra room"},
            "Location",
        )
        changed = False
        reads = []

        def change_after_count(connection, cursor, statement, parameters, context, executemany):
            nonlocal changed
            if any(
                table in statement
                for table in ("FROM inspection_items", "FROM inspections", "FROM laboratories")
            ):
                reads.append(statement)
            if not changed and statement.startswith("SELECT COUNT(*) FROM inspection_items item"):
                changed = True
                with database.begin() as other:
                    if mutation == "update":
                        other.execute(
                            text(
                                "UPDATE inspection_items SET status='uploaded',version=2 "
                                "WHERE inspection_id=:id"
                            ),
                            {"id": value["id"]},
                        )
                    else:
                        # Synthetic concurrent insert tests isolation, not a public add-item API.
                        insert(
                            other,
                            "inspection_items",
                            tenant_id=tenant["tenant_id"],
                            inspection_id=value["id"],
                            laboratory_id=lab["id"],
                            template_item_id=template["items"][0]["id"],
                            location_id=extra_room["id"],
                        )

        event.listen(database, "after_cursor_execute", change_after_count)
        try:
            response = http.get(paths(value)[0] + f"?laboratory_id={lab['id']}")
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert changed and data["total"] == len(data["items"]) == 2
            assert {(i["status"], i["version"]) for i in data["items"]} == {("draft", 1)}
            assert any("FROM laboratories" in statement for statement in reads)
            assert all("FOR SHARE" not in s and "FOR UPDATE" not in s for s in reads)
        finally:
            event.remove(database, "after_cursor_execute", change_after_count)
        if mutation == "update":
            assert http.get(paths(value)[1]).json()["data"]["status"] == "uploaded"
        else:
            data = http.get(paths(value)[0]).json()["data"]
            assert data["total"] == len(data["items"]) == 3


@pytest.mark.parametrize("index", [0, 1])
def test_session_revoked_after_preflight_blocks_read_and_rate_limit_is_outside_transaction(
    identity, database, index
):
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
        value, _, _, _ = inspection(http, session)
        original_limit = service.limits.user

        def revoke(principal, *, write):
            assert active == 0 and write is False
            original_limit(principal, write=write)
            with database.begin() as connection:
                connection.execute(
                    text("UPDATE sessions SET revoked_at=UTC_TIMESTAMP(3) WHERE tenant_id=:tenant"),
                    {"tenant": tenant["tenant_id"]},
                )

        with (
            patch.object(service, "transaction", side_effect=tracked_transaction),
            patch.object(service.limits, "user", side_effect=revoke),
        ):
            assert http.get(paths(value)[index]).status_code == 401


def test_read_fallback_unauthenticated_and_revoked_role_boundaries(identity, database):
    tenant, service, app = identity
    with TestClient(app, base_url=ORIGIN) as admin, TestClient(app, base_url=ORIGIN) as reader:
        session = login(admin, tenant).json()
        value, lab, _, _ = inspection(admin, session)
        for path in paths(value):
            assert reader.get(path).status_code == 401
        user = create_user(admin, session)
        assert (
            role_write(
                admin, session, user["id"], command(role="viewer", lab=lab["id"])
            ).status_code
            == 200
        )
        assert login(reader, tenant, username="target").status_code == 200
        with patch.object(service.limits.redis, "eval", side_effect=ConnectionError):
            for path in paths(value):
                assert reader.get(path).status_code == 200
        assert (
            role_write(
                admin, session, user["id"], command(2, "viewer", lab["id"]), revoke=True
            ).status_code
            == 200
        )
        for path in paths(value):
            assert reader.get(path).status_code == 401
        assert login(reader, tenant, username="target").status_code == 200
        for path in paths(value):
            assert reader.get(path).status_code == 404
        assert count_rows(database, tenant, "inference_runs") == 0
