"""Snapshot consistency and boundary cases for I-02 command guards."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from packages.domain import workflow as w
from tests.domain import test_workflow as f
from tests.domain.test_workflow_matrix import ITEM_IMAGES, MATRIX, SELECTION, case, expect_error


@pytest.mark.parametrize("name", ["submit_item", "retry_item", "complete_item"])
@pytest.mark.parametrize("parent_state", ["completed", "cancelled", "unknown"])
def test_terminal_inspection_blocks_item_commands(name, parent_state):
    command, actor, target, siblings, args = case(name)
    expect_error(
        "STATE_CONFLICT",
        command,
        actor,
        replace(target, inspection_status=parent_state),
        *siblings,
        **args,
    )


@pytest.mark.parametrize("name", ["submit_item", "retry_item", "complete_item"])
@pytest.mark.parametrize(
    "change,code",
    [
        ({"tenant_id": "foreign"}, "NOT_FOUND"),
        ({"laboratory_id": "foreign"}, "NOT_FOUND"),
        ({"item_id": "foreign"}, "NOT_FOUND"),
        ({"run_id": "old"}, "STATE_CONFLICT"),
        ({"evaluation_id": "old"}, "STATE_CONFLICT"),
        ({"superseded": None}, "STATE_CONFLICT"),
        ({"status": "unknown"}, "STATE_CONFLICT"),
    ],
)
def test_current_finding_must_match_loaded_aggregate(name, change, code):
    command, actor, target, siblings, args = case(name)
    args["current_findings"] = (
        f.finding(status="rejected")
        if not change
        else replace(f.finding(status="rejected"), **change),
    )
    expect_error(code, command, actor, target, *siblings, **args)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"tenant_id": "foreign"}, "NOT_FOUND"),
        ({"id": "old"}, "STATE_CONFLICT"),
        ({"run_id": "old"}, "STATE_CONFLICT"),
        ({"item_id": "other"}, "STATE_CONFLICT"),
        ({"status": "queued"}, "STATE_CONFLICT"),
        ({"status": "failed"}, "STATE_CONFLICT"),
        ({"has_unknown": None}, "VALIDATION_ERROR"),
        ({"has_unknown": 1}, "VALIDATION_ERROR"),
        ({"insufficient_facts": "false"}, "VALIDATION_ERROR"),
    ],
)
def test_completion_uses_only_current_typed_evaluation(change, code):
    command, actor, target, siblings, args = case("complete_item")
    args["evaluation"] = f.evaluation(**change)
    expect_error(code, command, actor, target, *siblings, **args)


@pytest.mark.parametrize("outcome", ["unknown", None, True, 1])
def test_invalid_completion_outcome_is_state_conflict(outcome):
    command, actor, target, siblings, args = case("complete_item")
    args["outcome"] = outcome
    expect_error("STATE_CONFLICT", command, actor, target, *siblings, **args)


def test_superseded_uncertainty_is_not_current_and_duplicate_findings_are_rejected():
    command, actor, target, siblings, args = case("complete_item")
    args["current_findings"] = (
        f.finding(status="cannot_determine", superseded=True, run_id="old", evaluation_id="old"),
    )
    assert command(actor, target, *siblings, **args) is w.ItemStatus.COMPLETED
    args["current_findings"] = (f.finding(status="rejected"),) * 2
    expect_error("STATE_CONFLICT", command, actor, target, *siblings, **args)


@pytest.mark.parametrize("run_state", [*w.RunStatus, "unknown"])
def test_only_technical_failure_can_be_retried(run_state):
    command, actor, target, siblings, args = case("retry_item")
    args["old_run"] = f.failed_run(status=run_state)
    if run_state == "failed":
        assert command(actor, target, *siblings, **args).replay_of == "run-1"
    else:
        expect_error("STATE_CONFLICT", command, actor, target, *siblings, **args)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"tenant_id": "other"}, "NOT_FOUND"),
        ({"id": "old"}, "STATE_CONFLICT"),
        ({"item_id": "other"}, "STATE_CONFLICT"),
        ({"configuration_available": False}, "STATE_CONFLICT"),
        ({"configuration_available": 1}, "STATE_CONFLICT"),
    ],
)
def test_retry_rejects_foreign_stale_or_unavailable_run(change, code):
    command, actor, target, siblings, args = case("retry_item")
    args["old_run"] = f.failed_run(**change)
    expect_error(code, command, actor, target, *siblings, **args)


@pytest.mark.parametrize(
    "change",
    [
        {"model_bundle_id": ""},
        {"dictionary_version_id": None},
        {"rule_bundle_id": "  "},
        {"pipeline_version": "latest"},
        {"device_profile": "auto"},
        {"reference_date": "2026-09-30"},
        {"reference_date": datetime(2026, 9, 30, tzinfo=timezone.utc)},
    ],
)
def test_retry_never_falls_back_to_latest_configuration(change):
    command, actor, target, siblings, args = case("retry_item")
    args["old_run"] = f.failed_run(pinned=replace(f.failed_run().pinned, **change))
    expect_error("STATE_CONFLICT", command, actor, target, *siblings, **args)


@pytest.mark.parametrize("name", ["submit_item", "retry_item", "submit_evidence"])
def test_ready_but_unavailable_image_cannot_be_used(name):
    command, actor, target, siblings, args = case(name)
    args["images"] = (replace(args["images"][0], available=False),)
    expect_error("STATE_CONFLICT", command, actor, target, *siblings, **args)


@pytest.mark.parametrize("name,maximum", [("submit_item", 3), ("submit_evidence", 10)])
def test_image_count_exact_limit_and_overflow(name, maximum):
    command, actor, target, siblings, args = case(name)
    template = args["images"][0]
    for count in (1, maximum, maximum + 1):
        args["images"] = tuple(replace(template, id=str(i)) for i in range(count))
        if name == "submit_item":
            args["selections"] = tuple(
                w.ImageSelection(
                    image_id=str(i),
                    role="overview" if i == 0 else "detail",
                    parent_image_id=None if i == 0 else "0",
                )
                for i in range(count)
            )
        if count <= maximum:
            assert command(actor, target, *siblings, **args) is not None
        else:
            expect_error("VALIDATION_ERROR", command, actor, target, *siblings, **args)


def test_item_images_must_share_location():
    expect_error(
        "VALIDATION_ERROR",
        w.submit_item,
        f.INSPECTOR,
        f.item(status="uploaded"),
        expected_version=1,
        images=(replace(ITEM_IMAGES[0], location_id="other"),),
        selections=(SELECTION,),
        current_findings=(),
    )


@pytest.mark.parametrize(
    "name",
    [
        "accept_task",
        "submit_evidence",
        "recheck_task",
        "reject_task",
        "cannot_remediate",
        "reassign_task",
    ],
)
@pytest.mark.parametrize(
    "change,code",
    [
        ({"tenant_id": "other"}, "NOT_FOUND"),
        ({"laboratory_id": "other"}, "NOT_FOUND"),
        ({"id": "other"}, "STATE_CONFLICT"),
        ({"status": "closed"}, "STATE_CONFLICT"),
        ({"superseded": True}, "STATE_CONFLICT"),
    ],
)
def test_remediation_requires_its_current_dispatched_finding(name, change, code):
    command, actor, target, siblings, args = case(name)
    expect_error(code, command, actor, target, replace(siblings[0], **change), **args)


@pytest.mark.parametrize("name", ["recheck_task", "reject_task"])
@pytest.mark.parametrize(
    "change,code",
    [
        ({"tenant_id": "other"}, "NOT_FOUND"),
        ({"id": "old"}, "STATE_CONFLICT"),
        ({"task_id": "other"}, "STATE_CONFLICT"),
        ({"submitted_by": ""}, "STATE_CONFLICT"),
    ],
)
def test_recheck_evidence_must_match_task_and_latest_identity(name, change, code):
    command, actor, target, siblings, args = case(name)
    args["evidence"] = replace(f.evidence(), **change)
    expect_error(code, command, actor, target, *siblings, **args)


@pytest.mark.parametrize("name", ["recheck_task", "reject_task"])
@pytest.mark.parametrize(
    "change", [{"latest_evidence_id": None}, {"latest_evidence_id": "new"}, {"assignee_id": ""}]
)
def test_recheck_requires_complete_task_snapshot(name, change):
    command, actor, target, siblings, args = case(name)
    expect_error("STATE_CONFLICT", command, actor, replace(target, **change), *siblings, **args)


@pytest.mark.parametrize("reason_code", [*w.UNCERTAIN_REASONS, "MISSING_EXPIRY", " ", None])
def test_uncertainty_reason_code_is_exact_contract_enum(reason_code):
    args = dict(expected_version=1, reason="unresolved", reason_code=reason_code)
    if reason_code in w.UNCERTAIN_REASONS:
        assert (
            w.cannot_determine_finding(f.INSPECTOR, f.finding(), **args)
            is w.FindingStatus.CANNOT_DETERMINE
        )
    else:
        expect_error(
            "VALIDATION_ERROR", w.cannot_determine_finding, f.INSPECTOR, f.finding(), **args
        )


@pytest.mark.parametrize("version", [False, 0, -1, 2147483648, "1", 1.0, None])
def test_expected_version_is_strict_integer(version):
    command, actor, target, siblings, args = case("accept_task")
    args["expected_version"] = version
    expect_error("VALIDATION_ERROR", command, actor, target, *siblings, **args)


def test_review_does_not_require_creator_but_capture_does():
    assert (
        w.complete_item(
            f.OTHER_INSPECTOR,
            f.item(),
            expected_version=1,
            evaluation=f.evaluation(),
            current_findings=(),
            outcome="no_issue",
            reason="independent review",
        )
        is w.ItemStatus.COMPLETED
    )
    assert (
        w.submit_item(
            f.ADMIN,
            f.item(status="uploaded"),
            expected_version=1,
            images=ITEM_IMAGES,
            selections=(SELECTION,),
            current_findings=(),
        )
        is w.ItemStatus.QUEUED
    )


@pytest.mark.parametrize("name", MATRIX)
def test_successful_decision_does_not_mutate_input_snapshots(name):
    command, actor, target, siblings, args = case(name)
    original = repr((actor, target, siblings, args))
    command(actor, target, *siblings, **args)
    assert repr((actor, target, siblings, args)) == original


def test_deadline_comparison_accepts_different_timezone_for_same_instant():
    command, actor, target, siblings, args = case("dispatch_finding")
    args["due_at"] = (args["now"] + timedelta(seconds=1)).astimezone(timezone(timedelta(hours=8)))
    assert command(actor, target, *siblings, **args).finding_status is w.FindingStatus.DISPATCHED
