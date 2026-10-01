"""Command/state, authorization and IRR-02 matrices; no services or persistence."""

import json
import re
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from packages.domain import workflow as w
from packages.domain.security import Principal, ServiceError
from tests.domain import test_workflow as f

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
SELECTION = w.ImageSelection(image_id="image-1", role="overview", parent_image_id=None)
ITEM_IMAGES = (f.image("inspection_item", "item-1"),)
DISPATCHED = f.finding(id="finding-1", status=w.FindingStatus.DISPATCHED)


def case(name):
    """Build a valid server-loaded context for each public command guard."""
    args = {"expected_version": 1}
    sibling = ()
    if name == "submit_item":
        actor, target = f.INSPECTOR, f.item(status="uploaded")
        args.update(images=ITEM_IMAGES, selections=(SELECTION,), current_findings=())
    elif name == "retry_item":
        actor, target = f.INSPECTOR, f.item(status="failed")
        args.update(images=ITEM_IMAGES, old_run=f.failed_run(), current_findings=())
    elif name == "complete_item":
        actor, target = f.INSPECTOR, f.item()
        args.update(evaluation=f.evaluation(), current_findings=(), outcome="no_issue", reason="ok")
    elif name in {"confirm_finding", "reject_finding", "cannot_determine_finding"}:
        actor, target = f.INSPECTOR, f.finding()
        args.update(reason="reviewed")
        if name == "cannot_determine_finding":
            args.update(reason_code="other")
    elif name == "dispatch_finding":
        actor, target = f.DISPATCHER, f.finding(status="confirmed")
        args.update(
            assignee=f.REMEDIATOR, assignee_active=True, due_at=NOW + timedelta(days=1), now=NOW
        )
    else:
        sibling = (DISPATCHED,)
        target = f.task()
        if name == "accept_task":
            actor = f.REMEDIATOR
        elif name == "submit_evidence":
            actor, target = f.REMEDIATOR, f.task(status="in_progress")
            args.update(images=(f.image("remediation_task", "task-1"),), description="done")
        elif name in {"recheck_task", "reject_task"}:
            actor = f.INSPECTOR
            target = f.task(status="pending_recheck", latest_evidence_id="evidence-1")
            args.update(evidence=f.evidence(), evidence_id="evidence-1", reason="checked")
        elif name == "cannot_remediate":
            actor = f.ADMIN
            args.update(reason="no safe repair available")
        elif name == "reassign_task":
            actor = f.DISPATCHER
            args.update(
                assignee=f.OTHER_REMEDIATOR,
                assignee_active=True,
                due_at=NOW + timedelta(days=1),
                now=NOW,
                reason="new owner",
            )
        else:
            raise AssertionError(name)
    return getattr(w, name), actor, target, sibling, args


MATRIX = {
    "submit_item": (w.ItemStatus, {"uploaded", "needs_retake", "needs_review"}),
    "retry_item": (w.ItemStatus, {"failed"}),
    "complete_item": (w.ItemStatus, {"needs_review"}),
    "confirm_finding": (w.FindingStatus, {"needs_review"}),
    "reject_finding": (w.FindingStatus, {"needs_review"}),
    "cannot_determine_finding": (w.FindingStatus, {"needs_review"}),
    "dispatch_finding": (w.FindingStatus, {"confirmed"}),
    "accept_task": (w.TaskStatus, {"pending_dispatch"}),
    "submit_evidence": (w.TaskStatus, {"in_progress", "rejected"}),
    "recheck_task": (w.TaskStatus, {"pending_recheck"}),
    "reject_task": (w.TaskStatus, {"pending_recheck"}),
    "cannot_remediate": (
        w.TaskStatus,
        {"pending_dispatch", "in_progress", "pending_recheck", "rejected"},
    ),
    "reassign_task": (
        w.TaskStatus,
        {"pending_dispatch", "in_progress", "rejected", "cannot_remediate"},
    ),
}


def expect_error(code, command, *args, **kwargs):
    with pytest.raises(ServiceError) as exc:
        command(*args, **kwargs)
    assert exc.value.code == code
    assert (
        exc.value.status
        == {
            "NOT_FOUND": 404,
            "FORBIDDEN": 403,
            "VALIDATION_ERROR": 422,
            "STATE_CONFLICT": 409,
            "VERSION_CONFLICT": 409,
        }[code]
    )


@pytest.mark.parametrize(
    "name,state",
    [
        (name, state)
        for name, (enum, _) in MATRIX.items()
        for state in [*enum, "unrecognized", None]
    ],
)
def test_every_command_state_pair(name, state):
    command, actor, target, sibling, args = case(name)
    target = replace(target, status=state)
    if state in MATRIX[name][1]:
        assert command(actor, target, *sibling, **args) is not None
    else:
        expect_error("STATE_CONFLICT", command, actor, target, *sibling, **args)


@pytest.mark.parametrize("name", MATRIX)
@pytest.mark.parametrize(
    "variant,code",
    [
        ("tenant", "NOT_FOUND"),
        ("lab", "NOT_FOUND"),
        ("viewer", "FORBIDDEN"),
        ("version", "VERSION_CONFLICT"),
        ("boolean_version", "VALIDATION_ERROR"),
    ],
)
def test_every_command_enforces_scope_permission_and_version(name, variant, code):
    command, actor, target, sibling, args = case(name)
    if variant == "tenant":
        actor = replace(actor, tenant_id="foreign")
        args["expected_version"] = 99  # Must not leak the version of a foreign object.
    elif variant == "lab":
        actor = replace(actor, roles=(("inspector", "laboratory", "other-lab"),))
    elif variant == "viewer":
        actor = replace(actor, roles=(("viewer", "laboratory", f.LAB),))
    else:
        args["expected_version"] = True if variant == "boolean_version" else 2
    expect_error(code, command, actor, target, *sibling, **args)


@pytest.mark.parametrize("risk_state", [None, "rejected", "confirmed", "dispatched", "closed"])
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("insufficient", [False, True])
@pytest.mark.parametrize("uncertain_finding", [False, True])
@pytest.mark.parametrize("outcome", w.ReviewOutcome)
def test_complete_truth_table(risk_state, unknown, insufficient, uncertain_finding, outcome):
    findings = () if risk_state is None else (f.finding(id="risk", status=risk_state),)
    if uncertain_finding:
        findings += (f.finding(id="uncertain", status="cannot_determine"),)
    u = unknown or insufficient or uncertain_finding
    c = risk_state in {"confirmed", "dispatched", "closed"}
    valid = (
        (outcome == "cannot_determine" and u)
        or (outcome == "no_issue" and not u and not c)
        or (outcome == "issues_confirmed" and not u and c)
    )
    args = dict(
        expected_version=1,
        evaluation=f.evaluation(has_unknown=unknown, insufficient_facts=insufficient),
        current_findings=findings,
        outcome=outcome,
        reason="final decision",
    )
    if valid:
        assert w.complete_item(f.INSPECTOR, f.item(), **args) is w.ItemStatus.COMPLETED
    else:
        expect_error("STATE_CONFLICT", w.complete_item, f.INSPECTOR, f.item(), **args)


@pytest.mark.parametrize("outcome", [*w.ReviewOutcome, "unsafe", None])
def test_pending_finding_always_blocks_completion(outcome):
    expect_error(
        "STATE_CONFLICT",
        w.complete_item,
        f.INSPECTOR,
        f.item(),
        expected_version=1,
        evaluation=f.evaluation(has_unknown=True),
        current_findings=(f.finding(),),
        outcome=outcome,
        reason="pending",
    )


@pytest.mark.parametrize("name", ["submit_item", "retry_item"])
@pytest.mark.parametrize("status", ["confirmed", "dispatched", "closed"])
def test_current_risk_blocks_resubmission_and_retry(name, status):
    command, actor, target, sibling, args = case(name)
    args["current_findings"] = (f.finding(status=status),)
    expect_error("STATE_CONFLICT", command, actor, target, *sibling, **args)
    args["current_findings"] = (f.finding(status=status, superseded=True),)
    assert command(actor, target, *sibling, **args) is not None


@pytest.mark.parametrize(
    "name", ["confirm_finding", "reject_finding", "cannot_determine_finding", "dispatch_finding"]
)
def test_superseded_finding_rejects_all_commands(name):
    command, actor, target, sibling, args = case(name)
    expect_error(
        "STATE_CONFLICT", command, actor, replace(target, superseded=True), *sibling, **args
    )


@pytest.mark.parametrize("name", ["recheck_task", "reject_task"])
@pytest.mark.parametrize("identity", ["assignee", "submitter"])
@pytest.mark.parametrize("actor", [f.INSPECTOR, f.ADMIN])
def test_even_authorized_admin_or_inspector_cannot_recheck_own_work(name, identity, actor):
    command, _, target, sibling, args = case(name)
    if identity == "assignee":
        target = replace(target, assignee_id=actor.user_id)
    else:
        args["evidence"] = f.evidence(submitted_by=actor.user_id)
    expect_error("FORBIDDEN", command, actor, target, *sibling, **args)


@pytest.mark.parametrize("name", ["accept_task", "submit_evidence"])
@pytest.mark.parametrize("actor", [f.OTHER_REMEDIATOR, f.ADMIN])
def test_assignee_cannot_be_impersonated(name, actor):
    command, _, target, sibling, args = case(name)
    expect_error("FORBIDDEN", command, actor, target, *sibling, **args)


@pytest.mark.parametrize("name", ["submit_item", "submit_evidence"])
@pytest.mark.parametrize(
    "problem,code",
    [
        ("empty", "VALIDATION_ERROR"),
        ("duplicate", "VALIDATION_ERROR"),
        ("capacity", "VALIDATION_ERROR"),
        ("owner", "VALIDATION_ERROR"),
        ("owner_type", "VALIDATION_ERROR"),
        ("tenant", "NOT_FOUND"),
        ("lab", "NOT_FOUND"),
        ("unready", "STATE_CONFLICT"),
    ],
)
def test_image_lists_fail_closed(name, problem, code):
    command, actor, target, sibling, args = case(name)
    img = args["images"][0]
    if problem == "empty":
        args["images"] = ()
    elif problem == "duplicate":
        args["images"] = (img, img)
    elif problem == "capacity":
        args["images"] = tuple(replace(img, id=str(i)) for i in range(11))
    else:
        field, value = {
            "owner": ("owner_id", "other"),
            "owner_type": ("owner_type", "other"),
            "tenant": ("tenant_id", "foreign"),
            "lab": ("laboratory_id", "foreign"),
            "unready": ("status", "validating"),
        }[problem]
        args["images"] = (replace(img, **{field: value}),)
    expect_error(code, command, actor, target, *sibling, **args)


def test_image_selection_and_retry_retain_roles_parent_and_order():
    images = ITEM_IMAGES + (replace(ITEM_IMAGES[0], id="detail"),)
    selections = (
        SELECTION,
        w.ImageSelection(image_id="detail", role="detail", parent_image_id="image-1"),
    )
    assert (
        w.submit_item(
            f.INSPECTOR,
            f.item(status="uploaded"),
            expected_version=1,
            images=images,
            selections=selections,
            current_findings=(),
        )
        is w.ItemStatus.QUEUED
    )
    pinned = replace(f.failed_run().pinned, images=selections)
    decision = w.retry_item(
        f.INSPECTOR,
        f.item(status="failed"),
        expected_version=1,
        images=images,
        old_run=f.failed_run(pinned=pinned),
        current_findings=(),
    )
    assert decision.pinned is pinned
    assert decision.replay_of == "run-1"
    for bad in [
        (),
        selections[::-1],
        (SELECTION, SELECTION),
        (SELECTION, replace(selections[1], parent_image_id="foreign")),
        (replace(SELECTION, parent_image_id="image-1"), selections[1]),
        (SELECTION, replace(selections[1], role="overview")),
    ]:
        expect_error(
            "VALIDATION_ERROR",
            w.submit_item,
            f.INSPECTOR,
            f.item(status="uploaded"),
            expected_version=1,
            images=images,
            selections=bad,
            current_findings=(),
        )


@pytest.mark.parametrize("name", ["dispatch_finding", "reassign_task"])
@pytest.mark.parametrize("problem", ["disabled", "role", "wrong_lab", "naive", "equal", "past"])
def test_assignment_validation(name, problem):
    command, actor, target, sibling, args = case(name)
    if problem == "disabled":
        args["assignee_active"] = False
    elif problem in {"role", "wrong_lab"}:
        args["assignee"] = replace(
            f.REMEDIATOR,
            roles=(("viewer", "laboratory", f.LAB),)
            if problem == "role"
            else (("remediator", "laboratory", "other"),),
        )
    else:
        args["due_at"] = {
            "naive": NOW.replace(tzinfo=None),
            "equal": NOW,
            "past": NOW - timedelta(seconds=1),
        }[problem]
    expect_error("VALIDATION_ERROR", command, actor, target, *sibling, **args)


@pytest.mark.parametrize(
    "name",
    [
        "complete_item",
        "confirm_finding",
        "reject_finding",
        "cannot_determine_finding",
        "recheck_task",
        "reject_task",
        "cannot_remediate",
        "reassign_task",
    ],
)
@pytest.mark.parametrize("reason", ["", "  ", None, "x" * 2001])
def test_required_reasons(name, reason):
    command, actor, target, sibling, args = case(name)
    args["reason"] = reason
    expect_error("VALIDATION_ERROR", command, actor, target, *sibling, **args)


def test_schema_enum_and_capacity_consistency():
    root = Path(__file__).resolve().parents[2]
    schema = json.loads(
        (root / "packages/persistence/migrations/snapshots/0001_initial.json").read_text(
            encoding="utf-8"
        )
    )
    for table, enum in [
        ("inspection_items", w.ItemStatus),
        ("inference_runs", w.RunStatus),
        ("findings", w.FindingStatus),
        ("remediation_tasks", w.TaskStatus),
    ]:
        enum_sql = schema["tables"][table]["columns"]["status"]["type"]
        assert {value.value for value in enum} == set(re.findall("'([^']+)'", enum_sql))
    with (root / "contracts/public-api-v1.yaml").open(encoding="utf-8") as stream:
        spec = yaml.safe_load(stream)["components"]["schemas"]
    assert set(spec["FindingUncertain"]["properties"]["reason_code"]["enum"]) == w.UNCERTAIN_REASONS
    assert spec["ItemSubmit"]["properties"]["images"]["maxItems"] == 3
    assert spec["EvidenceSubmit"]["properties"]["image_ids"]["maxItems"] == 10


def test_snapshots_are_immutable_and_no_generic_status_setter_exists():
    with pytest.raises(FrozenInstanceError):
        f.item().status = "completed"
    assert not hasattr(w, "transition")
    assert not hasattr(w, "task_transition")
    assert f.failed_run().pinned.reference_date == date(2026, 9, 30)
    assert isinstance(f.INSPECTOR, Principal)
