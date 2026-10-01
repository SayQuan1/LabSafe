from datetime import date, datetime, timedelta, timezone

import pytest

from packages.domain.security import Principal, ServiceError
from packages.domain.workflow import (
    Evaluation,
    Evidence,
    FailedRun,
    Finding,
    FindingStatus,
    Image,
    ImageSelection,
    Item,
    ItemStatus,
    PinnedInputs,
    RemediationDecision,
    ReviewOutcome,
    RunStatus,
    Task,
    TaskStatus,
    accept_task,
    cannot_determine_finding,
    cannot_remediate,
    complete_item,
    confirm_finding,
    dispatch_finding,
    reassign_task,
    recheck_task,
    reject_finding,
    reject_task,
    retry_item,
    submit_evidence,
    submit_item,
)

TENANT = "tenant-1"
LAB = "lab-1"
INSPECTOR = Principal(
    user_id="inspector-1",
    tenant_id=TENANT,
    username="inspector",
    roles=(("inspector", "laboratory", LAB),),
)
ADMIN = Principal(
    user_id="admin-1",
    tenant_id=TENANT,
    username="admin",
    roles=(("safety_admin", "tenant", None),),
)
DISPATCHER = Principal(
    user_id="manager-1",
    tenant_id=TENANT,
    username="manager",
    roles=(("lab_manager", "laboratory", LAB),),
)
REMEDIATOR = Principal(
    user_id="remediator-1",
    tenant_id=TENANT,
    username="remediator",
    roles=(("remediator", "laboratory", LAB),),
)
OTHER_REMEDIATOR = Principal(
    user_id="remediator-2",
    tenant_id=TENANT,
    username="remediator2",
    roles=(("remediator", "laboratory", LAB),),
)
OTHER_INSPECTOR = Principal(
    user_id="inspector-2",
    tenant_id=TENANT,
    username="inspector2",
    roles=(("inspector", "laboratory", LAB),),
)


def resource(resource_type, **kwargs):
    common = dict(id="item-1", tenant_id=TENANT, laboratory_id=LAB, version=1)
    common.update(kwargs)
    return resource_type(**common)


def item(**kwargs):
    values = dict(
        status=ItemStatus.NEEDS_REVIEW,
        inspection_status="in_progress",
        creator_id=INSPECTOR.user_id,
        location_id="loc-1",
        current_run_id="run-1",
        current_evaluation_id="eval-1",
    )
    values.update(kwargs)
    return resource(Item, **values)


def finding(**kwargs):
    values = dict(
        status=FindingStatus.NEEDS_REVIEW,
        item_id="item-1",
        run_id="run-1",
        evaluation_id="eval-1",
        superseded=False,
        evidence_ready=True,
    )
    values.update(kwargs)
    return resource(Finding, **values)


def task(**kwargs):
    values = dict(
        id="task-1",
        status=TaskStatus.PENDING_DISPATCH,
        finding_id="finding-1",
        assignee_id=REMEDIATOR.user_id,
        latest_evidence_id=None,
    )
    values.update(kwargs)
    return resource(Task, **values)


def image(owner_type, owner_id, *, image_id="image-1", location_id="loc-1", status="ready"):
    return Image(
        id=image_id,
        tenant_id=TENANT,
        laboratory_id=LAB,
        owner_type=owner_type,
        owner_id=owner_id,
        location_id=location_id,
        status=status,
        available=True,
    )


def evidence(*, evidence_id="evidence-1", task_id="task-1", submitted_by=REMEDIATOR.user_id):
    return Evidence(
        id=evidence_id,
        tenant_id=TENANT,
        task_id=task_id,
        submitted_by=submitted_by,
    )


def evaluation(**kwargs):
    values = dict(
        id="eval-1",
        tenant_id=TENANT,
        item_id="item-1",
        run_id="run-1",
        status="completed",
        has_unknown=False,
        insufficient_facts=False,
    )
    values.update(kwargs)
    return Evaluation(**values)


def failed_run(**kwargs):
    values = dict(
        id="run-1",
        tenant_id=TENANT,
        item_id="item-1",
        status=RunStatus.FAILED,
        pinned=PinnedInputs(
            model_bundle_id="model",
            dictionary_version_id="dict",
            rule_bundle_id="rules",
            pipeline_version="vision-v1",
            device_profile="cpu",
            reference_date=date(2026, 9, 30),
            images=(ImageSelection(image_id="image-1", role="overview", parent_image_id=None),),
        ),
        configuration_available=True,
    )
    values.update(kwargs)
    return FailedRun(**values)


def call_error(callable_, *args, **kwargs):
    with pytest.raises(ServiceError) as exc:
        callable_(*args, **kwargs)
    return exc.value.code


def test_submit_requires_real_server_loaded_scope_and_ready_owned_images():
    assert (
        submit_item(
            INSPECTOR,
            item(status=ItemStatus.UPLOADED),
            expected_version=1,
            images=(image("inspection_item", "item-1"),),
            selections=(ImageSelection(image_id="image-1", role="overview", parent_image_id=None),),
            current_findings=(),
        )
        is ItemStatus.QUEUED
    )
    assert (
        call_error(
            submit_item,
            OTHER_INSPECTOR,
            item(status=ItemStatus.UPLOADED),
            expected_version=1,
            images=(image("inspection_item", "item-1"),),
            selections=(ImageSelection(image_id="image-1", role="overview", parent_image_id=None),),
            current_findings=(),
        )
        == "FORBIDDEN"
    )
    assert (
        call_error(
            submit_item,
            INSPECTOR,
            item(status=ItemStatus.UPLOADED),
            expected_version=1,
            images=(image("wrong", "item-1"),),
            selections=(ImageSelection(image_id="image-1", role="overview", parent_image_id=None),),
            current_findings=(),
        )
        == "VALIDATION_ERROR"
    )
    assert (
        call_error(
            submit_item,
            INSPECTOR,
            item(status=ItemStatus.QUEUED),
            expected_version=1,
            images=(image("inspection_item", "item-1"),),
            selections=(ImageSelection(image_id="image-1", role="overview", parent_image_id=None),),
            current_findings=(),
        )
        == "STATE_CONFLICT"
    )


def test_retry_preserves_failed_run_images_and_pinned_configuration():
    assert (
        retry_item(
            INSPECTOR,
            item(status=ItemStatus.FAILED),
            expected_version=1,
            old_run=failed_run(),
            images=(image("inspection_item", "item-1"),),
            current_findings=(),
        ).item_status
        is ItemStatus.QUEUED
    )
    assert (
        call_error(
            retry_item,
            INSPECTOR,
            item(status=ItemStatus.FAILED),
            expected_version=1,
            old_run=failed_run(configuration_available=False),
            images=(image("inspection_item", "item-1"),),
            current_findings=(),
        )
        == "STATE_CONFLICT"
    )
    assert (
        call_error(
            retry_item,
            INSPECTOR,
            item(status=ItemStatus.FAILED),
            expected_version=1,
            old_run=failed_run(),
            images=(image("inspection_item", "item-1", image_id="other"),),
            current_findings=(),
        )
        == "STATE_CONFLICT"
    )


def test_complete_uses_server_evaluation_and_three_valued_outcome():
    current = (finding(status=FindingStatus.REJECTED),)
    assert (
        complete_item(
            INSPECTOR,
            item(),
            expected_version=1,
            evaluation=evaluation(),
            current_findings=current,
            outcome=ReviewOutcome.NO_ISSUE,
            reason="reviewed",
        )
        is ItemStatus.COMPLETED
    )
    assert (
        call_error(
            complete_item,
            INSPECTOR,
            item(),
            expected_version=1,
            evaluation=evaluation(has_unknown=True),
            current_findings=current,
            outcome=ReviewOutcome.NO_ISSUE,
            reason="reviewed",
        )
        == "STATE_CONFLICT"
    )
    assert (
        complete_item(
            INSPECTOR,
            item(),
            expected_version=1,
            evaluation=evaluation(has_unknown=True),
            current_findings=current,
            outcome=ReviewOutcome.CANNOT_DETERMINE,
            reason="reviewed",
        )
        is ItemStatus.COMPLETED
    )
    assert (
        complete_item(
            INSPECTOR,
            item(),
            expected_version=1,
            evaluation=evaluation(),
            current_findings=(finding(status=FindingStatus.CONFIRMED),),
            outcome=ReviewOutcome.ISSUES_CONFIRMED,
            reason="risk retained",
        )
        is ItemStatus.COMPLETED
    )
    assert (
        call_error(
            complete_item,
            INSPECTOR,
            item(),
            expected_version=1,
            evaluation=evaluation(),
            current_findings=(finding(status=FindingStatus.NEEDS_REVIEW),),
            outcome=ReviewOutcome.NO_ISSUE,
            reason="not yet",
        )
        == "STATE_CONFLICT"
    )


def test_finding_review_requires_reason_evidence_and_rejects_superseded():
    assert (
        confirm_finding(INSPECTOR, finding(), expected_version=1, reason="evidence checked")
        is FindingStatus.CONFIRMED
    )
    assert (
        call_error(
            confirm_finding,
            INSPECTOR,
            finding(evidence_ready=False),
            expected_version=1,
            reason="review",
        )
        == "STATE_CONFLICT"
    )
    assert (
        reject_finding(INSPECTOR, finding(), expected_version=1, reason="false positive")
        is FindingStatus.REJECTED
    )
    assert (
        cannot_determine_finding(
            INSPECTOR,
            finding(),
            expected_version=1,
            reason="unreadable",
            reason_code="label_unreadable",
        )
        is FindingStatus.CANNOT_DETERMINE
    )
    assert (
        call_error(
            cannot_determine_finding,
            INSPECTOR,
            finding(),
            expected_version=1,
            reason="unknown",
            reason_code="bad",
        )
        == "VALIDATION_ERROR"
    )
    assert (
        call_error(
            confirm_finding, INSPECTOR, finding(superseded=True), expected_version=1, reason="old"
        )
        == "STATE_CONFLICT"
    )


def test_dispatch_creates_pending_task_and_rejects_cross_tenant_assignee():
    result = dispatch_finding(
        DISPATCHER,
        finding(status=FindingStatus.CONFIRMED),
        expected_version=1,
        assignee=REMEDIATOR,
        assignee_active=True,
        due_at=datetime(2026, 9, 30, tzinfo=timezone.utc) + timedelta(hours=1),
        now=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert result == RemediationDecision(TaskStatus.PENDING_DISPATCH, FindingStatus.DISPATCHED)
    foreign = Principal(
        user_id="foreign",
        tenant_id="tenant-2",
        username="foreign",
        roles=(("remediator", "laboratory", LAB),),
    )
    assert (
        call_error(
            dispatch_finding,
            DISPATCHER,
            finding(status=FindingStatus.CONFIRMED),
            expected_version=1,
            assignee=foreign,
            assignee_active=True,
            due_at=datetime(2026, 9, 30, tzinfo=timezone.utc) + timedelta(hours=1),
            now=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
        == "NOT_FOUND"
    )


def test_remediation_lifecycle_uses_actual_actor_and_latest_evidence():
    t = task()
    f = finding(id="finding-1", status=FindingStatus.DISPATCHED)
    assert accept_task(REMEDIATOR, t, f, expected_version=1) == RemediationDecision(
        TaskStatus.IN_PROGRESS, FindingStatus.DISPATCHED
    )
    imgs = (image("remediation_task", "task-1"),)
    assert submit_evidence(
        REMEDIATOR,
        task(status=TaskStatus.IN_PROGRESS),
        f,
        expected_version=1,
        images=imgs,
        description="after cleanup",
    ) == RemediationDecision(TaskStatus.PENDING_RECHECK, FindingStatus.DISPATCHED)
    pending = task(status=TaskStatus.PENDING_RECHECK, latest_evidence_id="evidence-1")
    ev = evidence()
    assert recheck_task(
        OTHER_INSPECTOR,
        pending,
        f,
        expected_version=1,
        evidence=ev,
        evidence_id="evidence-1",
        reason="independent check",
    ) == RemediationDecision(TaskStatus.CLOSED, FindingStatus.CLOSED)
    assert (
        call_error(
            recheck_task,
            REMEDIATOR,
            pending,
            f,
            expected_version=1,
            evidence=ev,
            evidence_id="evidence-1",
            reason="self check",
        )
        == "FORBIDDEN"
    )
    assert (
        call_error(
            recheck_task,
            OTHER_INSPECTOR,
            pending,
            f,
            expected_version=1,
            evidence=ev,
            evidence_id="wrong",
            reason="wrong evidence",
        )
        == "STATE_CONFLICT"
    )
    assert reject_task(
        OTHER_INSPECTOR,
        pending,
        f,
        expected_version=1,
        evidence=ev,
        evidence_id="evidence-1",
        reason="insufficient cleanup",
    ) == RemediationDecision(TaskStatus.REJECTED, FindingStatus.DISPATCHED)


def test_admin_cannot_remediate_keeps_finding_open_and_reassignment_requires_future_due():
    t = task(status=TaskStatus.IN_PROGRESS)
    f = finding(id="finding-1", status=FindingStatus.DISPATCHED)
    assert cannot_remediate(
        ADMIN, t, f, expected_version=1, reason="vendor unavailable"
    ) == RemediationDecision(TaskStatus.CANNOT_REMEDIATE, FindingStatus.DISPATCHED)
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert reassign_task(
        DISPATCHER,
        t,
        f,
        expected_version=1,
        assignee=OTHER_REMEDIATOR,
        assignee_active=True,
        due_at=now + timedelta(hours=1),
        now=now,
        reason="capacity change",
    ) == RemediationDecision(TaskStatus.PENDING_DISPATCH, FindingStatus.DISPATCHED)
    assert (
        call_error(
            reassign_task,
            DISPATCHER,
            t,
            f,
            expected_version=1,
            assignee=OTHER_REMEDIATOR,
            assignee_active=True,
            due_at=now,
            now=now,
            reason="late",
        )
        == "VALIDATION_ERROR"
    )


def test_version_conflict_and_run_enum():
    with pytest.raises(ServiceError) as exc:
        submit_item(
            INSPECTOR,
            item(status=ItemStatus.UPLOADED),
            expected_version=2,
            images=(image("inspection_item", "item-1"),),
            selections=(ImageSelection(image_id="image-1", role="overview", parent_image_id=None),),
            current_findings=(),
        )
    assert exc.value.code == "VERSION_CONFLICT"
    assert {status.value for status in RunStatus} == {
        "queued",
        "processing",
        "retrying",
        "completed",
        "needs_retake",
        "needs_review",
        "failed",
        "superseded",
    }
