"""Report snapshot, authorization and atomic replay on disposable MySQL only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from packages.domain.security import Principal, ServiceError
from packages.persistence import reports
from packages.persistence.dispatch import decoded, validate_dispatch
from packages.persistence.users import timestamp
from tests.persistence.factories import Graph, insert
from tests.persistence.test_identity_mysql import ORIGIN, assert_schema, headers, login
from tests.persistence.test_roles_mysql import command, create_user, lab_fixture, role_write


def body(labs):
    now = datetime.utcnow()
    return {
        "format": "csv",
        "filters": {
            "laboratory_ids": labs,
            "from": timestamp(now - timedelta(days=1)),
            "to": timestamp(now + timedelta(days=1)),
            "severity": [],
            "finding_status": [],
        },
    }


@pytest.fixture
def graph(transaction):
    graph = Graph(transaction)
    for item, run, fact, evaluation in (
        (graph.item, graph.run, graph.fact, graph.evaluation),
        (graph.other_item, graph.other_run, graph.other_fact, graph.other_evaluation),
    ):
        transaction.execute(
            text(
                "UPDATE inspection_items SET current_run_id=:run,"
                "current_fact_revision_id=:fact,current_evaluation_id=:evaluation WHERE id=:id"
            ),
            {"id": item, "run": run, "fact": fact, "evaluation": evaluation},
        )
    return graph


def snapshot(connection, export_id):
    return decoded(
        connection.scalar(
            text("SELECT snapshot FROM report_exports WHERE id=:id"), {"id": export_id}
        )
    )


def admin(graph):
    return Principal(graph.user, graph.tenant, "synthetic", (("safety_admin", "tenant", None),))


def create(connection, graph, request=None):
    return reports.create(connection, admin(graph), request or body([graph.lab]), str(uuid4()))


def test_report_export_snapshot_and_outbox_binding(transaction, graph):
    result = create(transaction, graph)
    frozen = snapshot(transaction, result["id"])
    assert frozen["export_id"] == result["id"] and frozen["tenant_id"] == graph.tenant
    assert frozen["laboratory_ids"] == [graph.lab]
    assert len(frozen["rows"]) == 2
    finding = next(row for row in frozen["rows"] if row["finding_id"])
    assert finding["fact_revision_id"] == graph.fact and finding["run_id"] == graph.run
    assert finding["rule_bundle_id"] == graph.rules
    assert finding["task_status"] == "pending_dispatch"
    assert {row["item_id"] for row in frozen["rows"]} == {graph.item, graph.other_item}
    assert all(set(row) == set(frozen["columns"]) for row in frozen["rows"])
    envelopes = (
        transaction.execute(
            text("SELECT event_type,payload FROM outbox_events WHERE tenant_id=:tenant"),
            {"tenant": graph.tenant},
        )
        .mappings()
        .all()
    )
    assert {e["event_type"] for e in envelopes} == {"TaskDispatch", "ReportRequested"}
    dispatch = next(decoded(e["payload"]) for e in envelopes if e["event_type"] == "TaskDispatch")
    assert validate_dispatch(dispatch)["payload"] == {"export_id": result["id"]}
    assert dispatch["task_id"] == result["job_id"]
    transaction.execute(
        text("UPDATE findings SET explanation='changed' WHERE id=:id"), {"id": graph.finding}
    )
    assert snapshot(transaction, result["id"]) == frozen


def test_report_export_filters_exclude_history_and_keep_zero_finding_items(transaction, graph):
    transaction.execute(
        text("UPDATE findings SET severity='critical',status='confirmed' WHERE id=:id"),
        {"id": graph.finding},
    )
    request = body([graph.lab])
    request["filters"]["severity"] = ["low"]
    result = create(transaction, graph, request)
    assert [r["item_id"] for r in snapshot(transaction, result["id"])["rows"]] == [graph.other_item]
    transaction.execute(
        text("UPDATE findings SET superseded_at=UTC_TIMESTAMP(3) WHERE id=:id"),
        {"id": graph.finding},
    )
    result = create(transaction, graph)
    assert all(r["finding_id"] is None for r in snapshot(transaction, result["id"])["rows"])


def test_report_export_exact_10000_finding_limit(transaction, graph):
    values = [
        {
            "id": str(uuid4()),
            "tenant": graph.tenant,
            "item": graph.item,
            "run": graph.run,
            "fact": graph.fact,
            "evaluation": graph.evaluation,
            "fingerprint": uuid4().hex * 2,
        }
        for _ in range(10000)
    ]
    sql = text(
        "INSERT INTO findings (id,tenant_id,item_id,run_id,fact_revision_id,rule_evaluation_id,"
        "rule_id,fingerprint,type,explanation,evidence) "
        "VALUES (:id,:tenant,:item,:run,:fact,:evaluation,'synthetic',"
        ":fingerprint,'synthetic','risk','[]')"
    )
    transaction.execute(sql, values[:9999])
    result = create(transaction, graph)
    assert len(snapshot(transaction, result["id"])["rows"]) == 10001  # Includes one empty item.
    transaction.execute(sql, values[9999:])
    with pytest.raises(ServiceError) as error:
        create(transaction, graph)
    assert error.value.status == 422
    assert (
        transaction.scalar(
            text("SELECT COUNT(*) FROM report_exports WHERE tenant_id=:tenant"),
            {"tenant": graph.tenant},
        )
        == 1
    )


@pytest.mark.parametrize(
    "role,scope,expected",
    [
        ("lab_manager", "own", 202),
        ("viewer", "own", 403),
        ("lab_manager", "other", 404),
        ("safety_admin", "foreign_tenant", 404),
    ],
)
def test_report_export_repository_scope(transaction, graph, role, scope, expected):
    request = body([graph.lab])
    tenant = str(uuid4()) if scope == "foreign_tenant" else graph.tenant
    roles = (
        ((role, "tenant", None),)
        if role == "safety_admin"
        else ((role, "laboratory", str(uuid4()) if scope == "other" else graph.lab),)
    )
    actor = Principal(graph.user, tenant, "synthetic", roles)
    if expected == 202:
        result = reports.create(transaction, actor, request, str(uuid4()))
        assert reports.get(transaction, actor, result["id"])["status"] == "queued"
    else:
        with pytest.raises(ServiceError) as error:
            reports.create(transaction, actor, request, str(uuid4()))
        assert error.value.status == expected


@pytest.fixture
def http_case(identity, database):
    tenant, service, app = identity
    lab = lab_fixture(database, tenant)
    with TestClient(app, base_url=ORIGIN) as http:
        session = login(http, tenant).json()
        yield tenant, service, http, session, body([lab])


def post(http, session, request, key="report-create-key"):
    return http.post("/api/v1/reports/exports", json=request, headers=headers(session, key))


def tables(database, tenant):
    with database.connect() as connection:
        return {
            name: [
                dict(r)
                for r in connection.execute(
                    text(f"SELECT * FROM {name} WHERE tenant_id=:tenant ORDER BY id"),
                    {"tenant": tenant},
                ).mappings()
            ]
            for name in (
                "report_exports",
                "task_runs",
                "task_attempts",
                "outbox_events",
                "audit_events",
                "api_idempotency",
            )
        }


def test_report_export_http_schema_idempotency_conflict_and_lab_manager(http_case, database):
    tenant, _, http, session, request = http_case
    user = create_user(http, session, "reporter")
    assert (
        role_write(
            http,
            session,
            user["id"],
            command(role="lab_manager", lab=request["filters"]["laboratory_ids"][0]),
        ).status_code
        == 200
    )
    session = login(http, tenant, username="reporter").json()
    first = post(http, session, request)
    assert first.status_code == 202, first.text
    assert_schema(first, "ReportExport")
    before = tables(database, tenant["tenant_id"])
    assert post(http, session, request).content == first.content
    assert tables(database, tenant["tenant_id"]) == before
    assert post(http, session, {**request, "format": "pdf"}).status_code == 409
    report_id, job_id = first.json()["data"]["id"], first.json()["data"]["job_id"]
    read = http.get(f"/api/v1/reports/exports/{report_id}")
    assert read.status_code == 200
    assert_schema(read, "ReportExport")
    assert_schema(http.get(f"/api/v1/jobs/{job_id}"), "Job")
    with database.begin() as connection:
        connection.execute(
            text("DELETE FROM user_roles WHERE tenant_id=:tenant AND user_id=:id"),
            {"tenant": tenant["tenant_id"], "id": user["id"]},
        )
    assert post(http, session, request).status_code == 404
    assert http.get(f"/api/v1/reports/exports/{report_id}").status_code == 404


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO report_exports",
        "INSERT INTO task_runs",
        "INSERT INTO outbox_events",
        "INSERT INTO audit_events",
        "UPDATE api_idempotency",
    ],
)
def test_report_export_create_write_failure_rolls_back(http_case, database, statement):
    tenant, _, http, session, request = http_case
    before = tables(database, tenant["tenant_id"])

    def fail(connection, cursor, sql, params, context, many):
        if sql.startswith(statement):
            raise RuntimeError("injected report write failure")

    event.listen(database, "before_cursor_execute", fail)
    try:
        response = post(http, session, request)
    finally:
        event.remove(database, "before_cursor_execute", fail)
    assert response.status_code == 500
    assert tables(database, tenant["tenant_id"]) == before


def make_failed(database, result):
    with database.begin() as connection:
        tenant = connection.scalar(
            text("SELECT tenant_id FROM task_runs WHERE id=:id"), {"id": result["job_id"]}
        )
        insert(
            connection,
            "task_attempts",
            tenant_id=tenant,
            task_id=result["job_id"],
            fencing_token=4,
            attempt=4,
            status="failed",
            error_code="INTERNAL_ERROR",
            finished_at=datetime.utcnow(),
        )
        connection.execute(
            text(
                "UPDATE report_exports SET status='failed',last_error_code='INTERNAL_ERROR',"
                "version=3 WHERE id=:id"
            ),
            {"id": result["id"]},
        )
        connection.execute(
            text(
                "UPDATE task_runs SET state='dead_letter',attempt=4,version=7,"
                "fencing_token=4,last_error_code='INTERNAL_ERROR' WHERE id=:id"
            ),
            {"id": result["job_id"]},
        )


def test_report_export_job_replay_and_list_are_atomic(http_case, database):
    tenant, _, http, session, request = http_case
    first = post(http, session, request)
    assert first.status_code == 202, first.text
    result = first.json()["data"]
    make_failed(database, result)
    before = tables(database, tenant["tenant_id"])
    listed = http.get(
        "/api/v1/dead-letters",
        params={
            "laboratory_id": request["filters"]["laboratory_ids"][0],
        },
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["items"][0]["id"] == result["job_id"]
    path = f"/api/v1/dead-letters/{result['job_id']}/replay"
    response = http.post(
        path,
        json={"expected_version": 7, "reason": "retry"},
        headers=headers(session, "report-replay-key"),
    )
    assert response.status_code == 202, response.text
    assert_schema(response, "Job")
    assert response.json()["data"]["replay_generation"] == 1
    replayed = http.post(
        path,
        json={"expected_version": 7, "reason": "retry"},
        headers=headers(session, "report-replay-key"),
    )
    assert replayed.content == response.content
    after = tables(database, tenant["tenant_id"])
    assert after["report_exports"][0]["status"] == "queued"
    assert after["report_exports"][0]["snapshot"] == before["report_exports"][0]["snapshot"]
    assert after["task_runs"][0]["fencing_token"] == 5
    assert after["task_runs"][0]["attempt"] == 0
    assert after["task_attempts"] == before["task_attempts"]
    assert len(after["outbox_events"]) == len(before["outbox_events"]) + 1


@pytest.mark.parametrize("fault", ["version", "ready", "snapshot", "payload"])
def test_report_export_replay_rejects_invalid_source(http_case, database, fault):
    tenant, _, http, session, request = http_case
    first = post(http, session, request)
    assert first.status_code == 202, first.text
    result = first.json()["data"]
    make_failed(database, result)
    if fault != "version":
        sql = {
            "ready": "UPDATE report_exports SET status='ready' WHERE id=:id",
            "snapshot": "UPDATE report_exports SET snapshot='{}' WHERE id=:id",
            "payload": "UPDATE task_runs SET payload='{}' WHERE id=:id",
        }[fault]
        with database.begin() as connection:
            connection.execute(
                text(sql), {"id": result["job_id"] if fault == "payload" else result["id"]}
            )
    before = tables(database, tenant["tenant_id"])
    response = http.post(
        f"/api/v1/dead-letters/{result['job_id']}/replay",
        json={"expected_version": 6 if fault == "version" else 7, "reason": "retry"},
        headers=headers(session, "report-replay-key"),
    )
    assert response.status_code == 409, response.text
    assert tables(database, tenant["tenant_id"]) == before


def test_report_export_replay_failure_restores_failed_report(http_case, database):
    tenant, _, http, session, request = http_case
    first = post(http, session, request)
    assert first.status_code == 202, first.text
    result = first.json()["data"]
    make_failed(database, result)
    before = tables(database, tenant["tenant_id"])
    with patch("packages.persistence.jobs.append_audit", side_effect=RuntimeError("fault")):
        response = http.post(
            f"/api/v1/dead-letters/{result['job_id']}/replay",
            json={"expected_version": 7, "reason": "retry"},
            headers=headers(session, "report-replay-key"),
        )
    assert response.status_code == 500
    assert tables(database, tenant["tenant_id"]) == before


def test_report_export_concurrent_same_key_across_sessions(http_case, database):
    tenant, service, http, _, request = http_case
    barrier = Barrier(2)
    with TestClient(http.app, base_url=ORIGIN) as second:
        sessions = [login(client, tenant).json() for client in (http, second)]
        tokens = [client.cookies.get("labsafe_session") for client in (http, second)]

        def create_one(index):
            barrier.wait(timeout=10)
            return http.app.state.reports.create(
                tokens[index],
                sessions[index]["data"]["csrf_token"],
                "concurrent-export",
                request,
                str(uuid4()),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(create_one, range(2)))
    assert responses[0] == responses[1] and responses[0][0] == 202
    rows = tables(database, tenant["tenant_id"])
    assert len(rows["report_exports"]) == len(rows["task_runs"]) == 1
    assert len(rows["outbox_events"]) == 2


def test_report_export_all_labs_required_even_when_snapshot_is_empty(transaction, graph):
    another = graph.add("laboratories", college_id=graph.college)
    result = create(transaction, graph, body([graph.lab, another]))
    partial = Principal(graph.user, graph.tenant, "reader", (("viewer", "laboratory", graph.lab),))
    with pytest.raises(ServiceError) as error:
        reports.get(transaction, partial, result["id"])
    assert error.value.status == 404
