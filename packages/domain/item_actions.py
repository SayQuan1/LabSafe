"""Read-only action probes using the exact command guards, never a second state table.

Contexts are server-loaded snapshots, not request bodies. None means unknown,
whereas () means the loader verified an exhaustive empty collection. Enabled
operations must also have real application/HTTP command handlers.
"""

from dataclasses import dataclass

from packages.domain.security import Permission, Principal, ServiceError, authorize, not_found
from packages.domain.workflow import (
    Evaluation,
    FailedRun,
    Finding,
    Image,
    ImageSelection,
    Item,
    ReviewOutcome,
    complete_item,
    retry_item,
    submit_item,
)

SUBMIT = "submitInspectionItem"
RETRY = "retryInspectionItem"
COMPLETE = "completeInspectionItem"
ITEM_OPERATIONS = frozenset({SUBMIT, RETRY, COMPLETE})


@dataclass(frozen=True, kw_only=True)
class ItemActionContext:
    current_findings: tuple[Finding, ...] | None = None
    images: tuple[Image, ...] | None = None
    selections: tuple[ImageSelection, ...] | None = None
    old_run: FailedRun | None = None
    evaluation: Evaluation | None = None


def allowed_item_actions(
    actor: Principal, item: Item, context: ItemActionContext, *, enabled: frozenset[str]
) -> list[str]:
    if actor.tenant_id != item.tenant_id:
        raise not_found()
    authorize(actor, Permission.READ, item.laboratory_id)
    if not enabled <= ITEM_OPERATIONS:
        raise ValueError("Unrecognized item command capability")
    if context.current_findings is None:
        return []  # An unloaded collection is not proof of no confirmed/unreviewed findings.
    shared = {"expected_version": item.version, "current_findings": context.current_findings}
    probes = {}
    if SUBMIT in enabled and context.images is not None and context.selections is not None:
        probes[SUBMIT] = [
            lambda: submit_item(
                actor, item, **shared, images=context.images, selections=context.selections
            )
        ]
    if RETRY in enabled and context.images is not None and context.old_run is not None:
        probes[RETRY] = [
            lambda: retry_item(
                actor, item, **shared, images=context.images, old_run=context.old_run
            )
        ]
    if COMPLETE in enabled and context.evaluation is not None:
        # Test existence of a legal outcome, not an invented outcome in the response.
        # Actual commands must validate the submitted outcome, reason and version again.
        probes[COMPLETE] = [
            lambda outcome=outcome: complete_item(
                actor,
                item,
                **shared,
                evaluation=context.evaluation,
                outcome=outcome,
                reason="Read-only action availability probe",
            )
            for outcome in ReviewOutcome
        ]
    allowed = []
    for operation in sorted(probes):
        for probe in probes[operation]:
            try:
                probe()
            except ServiceError as error:
                if error.status not in {403, 404, 409, 422}:
                    raise
            else:
                allowed.append(operation)
                break
    return allowed
