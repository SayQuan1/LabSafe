from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.persistence.factories import Graph, insert


@pytest.fixture
def graph(transaction):
    return Graph(transaction)


def rejected(code, operation):
    with pytest.raises(DBAPIError) as failure:
        operation()
    assert failure.value.orig.args[0] == code


def test_cross_tenant_foreign_key(transaction, graph):
    other_tenant = insert(transaction, "tenants", timezone="UTC")
    rejected(
        1452,
        lambda: insert(
            transaction, "laboratories", tenant_id=other_tenant, college_id=graph.college
        ),
    )


@pytest.mark.parametrize(
    "column,wrong,right",
    [
        ("current_run_id", "other_run", "run"),
        ("current_fact_revision_id", "other_fact", "fact"),
        ("current_evaluation_id", "other_evaluation", "evaluation"),
    ],
)
def test_current_pointer_must_belong_to_same_item(transaction, graph, column, wrong, right):
    statement = text(f"UPDATE inspection_items SET {column}=:target WHERE id=:item")
    rejected(
        1452,
        lambda: transaction.execute(
            statement, {"target": getattr(graph, wrong), "item": graph.item}
        ),
    )
    transaction.execute(statement, {"target": getattr(graph, right), "item": graph.item})


def test_nullable_scope_cannot_duplicate_tenant_role(transaction, graph):
    role = insert(transaction, "roles")
    values = dict(user_id=graph.user, role_id=role, scope_kind="tenant")
    graph.add("user_roles", **values)
    rejected(1062, lambda: graph.add("user_roles", **values))
    rejected(3819, lambda: graph.add("user_roles", **values, laboratory_id=graph.lab))
    rejected(
        3819,
        lambda: graph.add("user_roles", user_id=graph.user, role_id=role, scope_kind="laboratory"),
    )
    graph.add(
        "user_roles",
        user_id=graph.user,
        role_id=role,
        scope_kind="laboratory",
        laboratory_id=graph.lab,
    )


@pytest.mark.parametrize("table", ["uploads", "asset_images"])
@pytest.mark.parametrize("both", [False, True])
def test_exactly_one_owner_required(graph, table, both):
    owners = {"inspection_item_id": graph.item, "remediation_task_id": graph.task} if both else {}
    extra = {"requested_by": graph.user} if table == "uploads" else {"upload_id": graph.upload}
    rejected(3819, lambda: graph.add(table, laboratory_id=graph.lab, **extra, **owners))


def test_remediation_owner_is_valid(graph):
    upload = graph.add(
        "uploads", laboratory_id=graph.lab, remediation_task_id=graph.task, requested_by=graph.user
    )
    graph.add(
        "asset_images", laboratory_id=graph.lab, remediation_task_id=graph.task, upload_id=upload
    )


def test_multi_crop_allowed_duplicate_run_crop_rejected(graph):
    first = str(uuid4())
    graph.add("image_derivatives", run_id=graph.run, image_id=graph.image, crop_id=first)
    graph.add("image_derivatives", run_id=graph.run, image_id=graph.image, crop_id=str(uuid4()))
    rejected(
        1062,
        lambda: graph.add(
            "image_derivatives", run_id=graph.run, image_id=graph.image, crop_id=first
        ),
    )
    rejected(
        1452,
        lambda: graph.add(
            "image_derivatives", run_id=graph.other_run, image_id=graph.image, crop_id=str(uuid4())
        ),
    )


def test_run_image_and_parent_must_belong_to_same_run_item(graph):
    rejected(
        1452,
        lambda: graph.add(
            "run_images", item_id=graph.other_item, run_id=graph.other_run, image_id=graph.image
        ),
    )
    upload = graph.add(
        "uploads",
        laboratory_id=graph.lab,
        inspection_item_id=graph.other_item,
        requested_by=graph.user,
    )
    image = graph.add(
        "asset_images",
        laboratory_id=graph.lab,
        inspection_item_id=graph.other_item,
        upload_id=upload,
    )
    rejected(
        1452,
        lambda: graph.add(
            "run_images",
            item_id=graph.other_item,
            run_id=graph.other_run,
            image_id=image,
            parent_image_id=graph.image,
        ),
    )
    graph.add("run_images", item_id=graph.other_item, run_id=graph.other_run, image_id=image)


def test_remediation_evidence_pointer_must_belong_to_same_task(transaction, graph):
    evidence = graph.add("remediation_evidence", task_id=graph.task, submitted_by=graph.user)
    finding = graph.add(
        "findings",
        item_id=graph.other_item,
        run_id=graph.other_run,
        fact_revision_id=graph.other_fact,
        rule_evaluation_id=graph.other_evaluation,
    )
    task = graph.add(
        "remediation_tasks", finding_id=finding, laboratory_id=graph.lab, assignee_id=graph.user
    )
    statement = text("UPDATE remediation_tasks SET latest_evidence_id=:evidence WHERE id=:task")
    rejected(1452, lambda: transaction.execute(statement, {"evidence": evidence, "task": task}))
    transaction.execute(statement, {"evidence": evidence, "task": graph.task})


def test_task_attempt_and_enum_guards(graph):
    graph.add("task_runs", attempt=4)
    rejected(3819, lambda: graph.add("task_runs", attempt=5))
    rejected(1265, lambda: graph.add("task_runs", state="not-a-real-state"))


def test_parent_delete_restricted(transaction, graph):
    rejected(
        1451,
        lambda: transaction.execute(text("DELETE FROM tenants WHERE id=:id"), {"id": graph.tenant}),
    )
