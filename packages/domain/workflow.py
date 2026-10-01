"""Pure I-02 command guards, not HTTP handlers or a persistence unit of work.

Snapshots MUST be loaded by the server in the same locked transaction. In
particular, current_findings must be exhaustive, not a client-provided subset.
Successful guards return decisions only; no snapshot or database is mutated.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TypeVar

from packages.domain.security import (
    Permission,
    Principal,
    ServiceError,
    authorize,
    not_found,
    require_version,
)


class ItemStatus(StrEnum):
    DRAFT = "draft"
    UPLOADED = "uploaded"
    QUEUED = "queued"
    QUALITY_CHECKING = "quality_checking"
    NEEDS_RETAKE = "needs_retake"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRYING = "retrying"
    COMPLETED = "completed"
    NEEDS_RETAKE = "needs_retake"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class FindingStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANNOT_DETERMINE = "cannot_determine"
    DISPATCHED = "dispatched"
    CLOSED = "closed"


class TaskStatus(StrEnum):
    PENDING_DISPATCH = "pending_dispatch"
    IN_PROGRESS = "in_progress"
    PENDING_RECHECK = "pending_recheck"
    REJECTED = "rejected"
    CANNOT_REMEDIATE = "cannot_remediate"
    CLOSED = "closed"


class ReviewOutcome(StrEnum):
    NO_ISSUE = "no_issue"
    ISSUES_CONFIRMED = "issues_confirmed"
    CANNOT_DETERMINE = "cannot_determine"


@dataclass(frozen=True, kw_only=True)
class Resource:
    id: str
    tenant_id: str
    laboratory_id: str
    version: int


@dataclass(frozen=True, kw_only=True)
class Item(Resource):
    status: ItemStatus | str
    inspection_status: str
    creator_id: str
    location_id: str
    current_run_id: str | None
    current_evaluation_id: str | None


@dataclass(frozen=True, kw_only=True)
class Finding(Resource):
    status: FindingStatus | str
    item_id: str
    run_id: str
    evaluation_id: str
    superseded: bool
    evidence_ready: bool


@dataclass(frozen=True, kw_only=True)
class Task(Resource):
    status: TaskStatus | str
    finding_id: str
    assignee_id: str
    latest_evidence_id: str | None


@dataclass(frozen=True, kw_only=True)
class Image:
    id: str
    tenant_id: str
    laboratory_id: str
    owner_type: str
    owner_id: str
    location_id: str | None
    status: str
    available: bool


@dataclass(frozen=True, kw_only=True)
class Evidence:
    id: str
    tenant_id: str
    task_id: str
    submitted_by: str


@dataclass(frozen=True, kw_only=True)
class Evaluation:
    id: str
    tenant_id: str
    item_id: str
    run_id: str
    status: str
    has_unknown: bool
    insufficient_facts: bool


@dataclass(frozen=True, kw_only=True)
class ImageSelection:
    image_id: str
    role: str
    parent_image_id: str | None


@dataclass(frozen=True, kw_only=True)
class PinnedInputs:
    """Immutable retry inputs; configuration availability is server-verified."""

    model_bundle_id: str
    dictionary_version_id: str
    rule_bundle_id: str
    pipeline_version: str
    device_profile: str
    reference_date: date
    images: tuple[ImageSelection, ...]


@dataclass(frozen=True, kw_only=True)
class FailedRun:
    id: str
    tenant_id: str
    item_id: str
    status: RunStatus | str
    pinned: PinnedInputs
    configuration_available: bool


@dataclass(frozen=True)
class RetryDecision:
    item_status: ItemStatus
    replay_of: str
    pinned: PinnedInputs


@dataclass(frozen=True)
class RemediationDecision:
    """Application MUST persist both statuses atomically, with audit/outbox."""

    task_status: TaskStatus
    finding_status: FindingStatus


EnumT = TypeVar("EnumT", bound=StrEnum)
CONFIRMED = frozenset({FindingStatus.CONFIRMED, FindingStatus.DISPATCHED, FindingStatus.CLOSED})
UNCERTAIN_REASONS = frozenset(
    {"blur", "occluded", "label_unreadable", "rule_missing", "insufficient_evidence", "other"}
)


def _conflict(message: str) -> ServiceError:
    return ServiceError("STATE_CONFLICT", 409, message)


def _invalid(message: str) -> ServiceError:
    return ServiceError("VALIDATION_ERROR", 422, message)


def _enum(value: str, kind: type[EnumT]) -> EnumT:
    try:
        return kind(value)
    except (ValueError, TypeError) as exc:
        raise _conflict("Unknown state or outcome") from exc


def _text(value: str, maximum: int = 2000) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _invalid("Nonblank text within the contract length limit is required")


def _guard(
    actor: Principal,
    resource: Resource,
    permission: Permission,
    expected_version: int,
    **identities: str,
) -> None:
    # Visibility precedes version/state checks, including for tenant administrators.
    if actor.tenant_id != resource.tenant_id:
        raise not_found()
    if not resource.id or not resource.laboratory_id:
        raise _conflict("Resource scope is incomplete")
    authorize(actor, permission, resource.laboratory_id, **identities)
    require_version(resource.version, expected_version)


def _current(finding: Finding) -> FindingStatus:
    if finding.superseded is not False:
        raise _conflict("Superseded findings are immutable")
    return _enum(finding.status, FindingStatus)


def _findings(item: Item, findings: tuple[Finding, ...]) -> tuple[FindingStatus, ...]:
    states = []
    seen = set()
    for finding in findings:
        if (
            finding.tenant_id != item.tenant_id
            or finding.laboratory_id != item.laboratory_id
            or finding.item_id != item.id
        ):
            raise not_found()
        if finding.id in seen:
            raise _conflict("Duplicate finding in aggregate snapshot")
        seen.add(finding.id)
        if finding.superseded is True:
            continue
        if (
            finding.run_id != item.current_run_id
            or finding.evaluation_id != item.current_evaluation_id
        ):
            raise _conflict("Finding does not belong to the current evaluation")
        states.append(_current(finding))
    return tuple(states)


def _open_inspection(item: Item) -> None:
    if item.inspection_status not in {"draft", "in_progress"}:
        raise _conflict("Inspection is not open")
    if not item.creator_id or not item.location_id:
        raise _conflict("Inspection ownership or location is incomplete")


def _images(
    resource: Resource,
    images: tuple[Image, ...],
    owner_type: str,
    maximum: int,
    location_id: str | None = None,
) -> None:
    if not 1 <= len(images) <= maximum or len({image.id for image in images}) != len(images):
        raise _invalid("Image list is empty, duplicated or over capacity")
    for image in images:
        if image.tenant_id != resource.tenant_id or image.laboratory_id != resource.laboratory_id:
            raise not_found()
        if image.owner_type != owner_type or image.owner_id != resource.id:
            raise _invalid("Image does not belong to this owner")
        if location_id is not None and image.location_id != location_id:
            raise _invalid("Image location differs from the item")
        if image.status != "ready" or image.available is not True:
            raise _conflict("All images must be ready and available")


def _selections(images: tuple[Image, ...], selections: tuple[ImageSelection, ...]) -> None:
    if tuple(image.id for image in images) != tuple(value.image_id for value in selections):
        raise _invalid("Loaded images must match the complete ordered selection")
    overview = selections[0]
    if overview.role != "overview" or overview.parent_image_id is not None:
        raise _invalid("The first image must be the sole parentless overview")
    if any(
        value.role != "detail" or value.parent_image_id != overview.image_id
        for value in selections[1:]
    ):
        raise _invalid("Detail images must reference the first overview")


def submit_item(
    actor: Principal,
    item: Item,
    *,
    expected_version: int,
    images: tuple[Image, ...],
    selections: tuple[ImageSelection, ...],
    current_findings: tuple[Finding, ...],
) -> ItemStatus:
    _guard(actor, item, Permission.CAPTURE, expected_version, creator_id=item.creator_id)
    _open_inspection(item)
    if _enum(item.status, ItemStatus) not in {
        ItemStatus.UPLOADED,
        ItemStatus.NEEDS_RETAKE,
        ItemStatus.NEEDS_REVIEW,
    }:
        raise _conflict("Item cannot be submitted")
    if CONFIRMED.intersection(_findings(item, current_findings)):
        raise _conflict("Confirmed risks block resubmission")
    _images(item, images, "inspection_item", 3, item.location_id)
    _selections(images, selections)
    return ItemStatus.QUEUED


def retry_item(
    actor: Principal,
    item: Item,
    *,
    expected_version: int,
    old_run: FailedRun,
    images: tuple[Image, ...],
    current_findings: tuple[Finding, ...],
) -> RetryDecision:
    _guard(actor, item, Permission.REVIEW, expected_version)
    _open_inspection(item)
    if old_run.tenant_id != item.tenant_id:
        raise not_found()
    if (
        _enum(item.status, ItemStatus) is not ItemStatus.FAILED
        or old_run.id != item.current_run_id
        or old_run.item_id != item.id
        or _enum(old_run.status, RunStatus) is not RunStatus.FAILED
    ):
        raise _conflict("Retry requires the current technically failed run")
    if CONFIRMED.intersection(_findings(item, current_findings)):
        raise _conflict("Confirmed risks block retry")
    pinned = old_run.pinned
    if old_run.configuration_available is not True or not all(
        isinstance(value, str) and value.strip()
        for value in (
            pinned.model_bundle_id,
            pinned.dictionary_version_id,
            pinned.rule_bundle_id,
            pinned.pipeline_version,
            pinned.device_profile,
        )
    ):
        raise _conflict("Pinned configuration is unavailable")
    if (
        pinned.pipeline_version != "vision-v1"
        or pinned.device_profile not in {"cpu", "cuda"}
        or type(pinned.reference_date) is not date
    ):
        raise _conflict("Pinned execution profile is invalid")
    _images(item, images, "inspection_item", 3, item.location_id)
    if tuple(image.id for image in images) != tuple(value.image_id for value in pinned.images):
        raise _conflict("Retry must preserve the original ordered image list")
    _selections(images, pinned.images)
    return RetryDecision(ItemStatus.QUEUED, old_run.id, pinned)


def complete_item(
    actor: Principal,
    item: Item,
    *,
    expected_version: int,
    evaluation: Evaluation,
    current_findings: tuple[Finding, ...],
    outcome: ReviewOutcome | str,
    reason: str,
) -> ItemStatus:
    _guard(actor, item, Permission.REVIEW, expected_version)
    _open_inspection(item)
    if _enum(item.status, ItemStatus) is not ItemStatus.NEEDS_REVIEW:
        raise _conflict("Only an item needing review can be completed")
    if evaluation.tenant_id != item.tenant_id:
        raise not_found()
    if (
        evaluation.id != item.current_evaluation_id
        or evaluation.item_id != item.id
        or evaluation.run_id != item.current_run_id
        or evaluation.status != "completed"
    ):
        raise _conflict("Current evaluation is not complete")
    if type(evaluation.has_unknown) is not bool or type(evaluation.insufficient_facts) is not bool:
        raise _invalid("Evaluation flags must be booleans")
    states = _findings(item, current_findings)
    if FindingStatus.NEEDS_REVIEW in states:
        raise _conflict("All current findings must be reviewed first")
    unknown = (
        evaluation.has_unknown
        or evaluation.insufficient_facts
        or FindingStatus.CANNOT_DETERMINE in states
    )
    confirmed = bool(CONFIRMED.intersection(states))
    required = (
        ReviewOutcome.CANNOT_DETERMINE
        if unknown
        else ReviewOutcome.ISSUES_CONFIRMED
        if confirmed
        else ReviewOutcome.NO_ISSUE
    )
    if _enum(outcome, ReviewOutcome) is not required:
        raise _conflict("Outcome does not match the current evaluation and findings")
    _text(reason)
    return ItemStatus.COMPLETED


def _review(actor: Principal, finding: Finding, expected_version: int, reason: str) -> None:
    _guard(actor, finding, Permission.REVIEW, expected_version)
    if _current(finding) is not FindingStatus.NEEDS_REVIEW:
        raise _conflict("Finding is not awaiting review")
    _text(reason)


def confirm_finding(
    actor: Principal, finding: Finding, *, expected_version: int, reason: str
) -> FindingStatus:
    _review(actor, finding, expected_version, reason)
    if finding.evidence_ready is not True:
        raise _conflict("Confirmation requires usable evidence")
    return FindingStatus.CONFIRMED


def reject_finding(
    actor: Principal, finding: Finding, *, expected_version: int, reason: str
) -> FindingStatus:
    _review(actor, finding, expected_version, reason)
    return FindingStatus.REJECTED


def cannot_determine_finding(
    actor: Principal, finding: Finding, *, expected_version: int, reason: str, reason_code: str
) -> FindingStatus:
    _review(actor, finding, expected_version, reason)
    if not isinstance(reason_code, str) or reason_code not in UNCERTAIN_REASONS:
        raise _invalid("Unknown uncertainty reason_code")
    return FindingStatus.CANNOT_DETERMINE


def _assignment(
    resource: Resource, assignee: Principal, assignee_active: bool, due_at: datetime, now: datetime
) -> None:
    if assignee.tenant_id != resource.tenant_id:
        raise not_found()
    if (
        assignee_active is not True
        or not assignee.user_id
        or ("remediator", "laboratory", resource.laboratory_id) not in assignee.roles
    ):
        raise _invalid("Assignee must be active with a remediator role in this laboratory")
    if any(
        not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
        for value in (due_at, now)
    ):
        raise _invalid("Deadline and clock must be timezone-aware")
    if due_at <= now:
        raise _invalid("due_at must be in the future")


def dispatch_finding(
    actor: Principal,
    finding: Finding,
    *,
    expected_version: int,
    assignee: Principal,
    assignee_active: bool,
    due_at: datetime,
    now: datetime,
) -> RemediationDecision:
    _guard(actor, finding, Permission.DISPATCH, expected_version)
    if _current(finding) is not FindingStatus.CONFIRMED:
        raise _conflict("Only confirmed findings can be dispatched")
    _assignment(finding, assignee, assignee_active, due_at, now)
    return RemediationDecision(TaskStatus.PENDING_DISPATCH, FindingStatus.DISPATCHED)


def _task(
    actor: Principal,
    task: Task,
    finding: Finding,
    permission: Permission,
    expected_version: int,
    **identities: str,
) -> TaskStatus:
    _guard(actor, task, permission, expected_version, **identities)
    if finding.tenant_id != task.tenant_id or finding.laboratory_id != task.laboratory_id:
        raise not_found()
    if finding.id != task.finding_id or _current(finding) is not FindingStatus.DISPATCHED:
        raise _conflict("Task must reference its current dispatched finding")
    return _enum(task.status, TaskStatus)


def accept_task(
    actor: Principal, task: Task, finding: Finding, *, expected_version: int
) -> RemediationDecision:
    state = _task(
        actor, task, finding, Permission.ASSIGNEE, expected_version, assignee_id=task.assignee_id
    )
    if state is not TaskStatus.PENDING_DISPATCH:
        raise _conflict("Task is not awaiting acceptance")
    return RemediationDecision(TaskStatus.IN_PROGRESS, FindingStatus.DISPATCHED)


def submit_evidence(
    actor: Principal,
    task: Task,
    finding: Finding,
    *,
    expected_version: int,
    images: tuple[Image, ...],
    description: str,
) -> RemediationDecision:
    state = _task(
        actor, task, finding, Permission.ASSIGNEE, expected_version, assignee_id=task.assignee_id
    )
    if state not in {TaskStatus.IN_PROGRESS, TaskStatus.REJECTED}:
        raise _conflict("Task is not accepting evidence")
    _images(task, images, "remediation_task", 10)
    _text(description, 4000)
    return RemediationDecision(TaskStatus.PENDING_RECHECK, FindingStatus.DISPATCHED)


def _recheck(
    actor: Principal,
    task: Task,
    finding: Finding,
    evidence: Evidence,
    evidence_id: str,
    expected_version: int,
    reason: str,
) -> None:
    _guard(actor, task, Permission.RECHECK, expected_version)
    if evidence.tenant_id != task.tenant_id:
        raise not_found()
    state = _task(
        actor,
        task,
        finding,
        Permission.RECHECK,
        expected_version,
        assignee_id=task.assignee_id,
        submitted_by=evidence.submitted_by,
    )
    if (
        state is not TaskStatus.PENDING_RECHECK
        or not task.latest_evidence_id
        or evidence_id != task.latest_evidence_id
        or evidence.id != evidence_id
        or evidence.task_id != task.id
        or not evidence.submitted_by
        or not task.assignee_id
    ):
        raise _conflict("Recheck requires the latest evidence for this task")
    _text(reason)


def recheck_task(
    actor: Principal,
    task: Task,
    finding: Finding,
    *,
    expected_version: int,
    evidence: Evidence,
    evidence_id: str,
    reason: str,
) -> RemediationDecision:
    _recheck(actor, task, finding, evidence, evidence_id, expected_version, reason)
    return RemediationDecision(TaskStatus.CLOSED, FindingStatus.CLOSED)


def reject_task(
    actor: Principal,
    task: Task,
    finding: Finding,
    *,
    expected_version: int,
    evidence: Evidence,
    evidence_id: str,
    reason: str,
) -> RemediationDecision:
    _recheck(actor, task, finding, evidence, evidence_id, expected_version, reason)
    return RemediationDecision(TaskStatus.REJECTED, FindingStatus.DISPATCHED)


def cannot_remediate(
    actor: Principal, task: Task, finding: Finding, *, expected_version: int, reason: str
) -> RemediationDecision:
    state = _task(actor, task, finding, Permission.ADMIN, expected_version)
    if state in {TaskStatus.CLOSED, TaskStatus.CANNOT_REMEDIATE}:
        raise _conflict("Task cannot be marked unremediable from this state")
    _text(reason)
    return RemediationDecision(TaskStatus.CANNOT_REMEDIATE, FindingStatus.DISPATCHED)


def reassign_task(
    actor: Principal,
    task: Task,
    finding: Finding,
    *,
    expected_version: int,
    assignee: Principal,
    assignee_active: bool,
    due_at: datetime,
    now: datetime,
    reason: str,
) -> RemediationDecision:
    state = _task(actor, task, finding, Permission.DISPATCH, expected_version)
    if state not in {
        TaskStatus.PENDING_DISPATCH,
        TaskStatus.IN_PROGRESS,
        TaskStatus.REJECTED,
        TaskStatus.CANNOT_REMEDIATE,
    }:
        raise _conflict("Task cannot be reassigned from this state")
    _assignment(task, assignee, assignee_active, due_at, now)
    _text(reason)
    return RemediationDecision(TaskStatus.PENDING_DISPATCH, FindingStatus.DISPATCHED)
