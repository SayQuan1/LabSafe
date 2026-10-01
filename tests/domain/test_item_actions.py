"""Action availability is a read-only probe of the original command guards."""

from dataclasses import replace
from unittest.mock import patch

import pytest

from packages.domain import item_actions as actions
from packages.domain.item_actions import (
    COMPLETE,
    ITEM_OPERATIONS,
    RETRY,
    SUBMIT,
    ItemActionContext,
    allowed_item_actions,
)
from packages.domain.security import ServiceError
from packages.domain.workflow import (
    ItemStatus,
    ReviewOutcome,
    complete_item,
    retry_item,
    submit_item,
)
from tests.domain.test_workflow import (
    ADMIN,
    DISPATCHER,
    INSPECTOR,
    OTHER_INSPECTOR,
    REMEDIATOR,
    evaluation,
    failed_run,
    finding,
    image,
    item,
)


def context(**overrides):
    values = dict(
        current_findings=(),
        images=(image("inspection_item", "item-1"),),
        selections=failed_run().pinned.images,
        old_run=failed_run(),
        evaluation=evaluation(),
    )
    values.update(overrides)
    return ItemActionContext(**values)


def direct_actions(actor, snapshot, loaded):
    """Independent reference: call the commands with their actual required arguments."""
    probes = [
        (
            SUBMIT,
            lambda: submit_item(
                actor,
                snapshot,
                expected_version=snapshot.version,
                current_findings=loaded.current_findings,
                images=loaded.images,
                selections=loaded.selections,
            ),
        ),
        (
            RETRY,
            lambda: retry_item(
                actor,
                snapshot,
                expected_version=snapshot.version,
                current_findings=loaded.current_findings,
                images=loaded.images,
                old_run=loaded.old_run,
            ),
        ),
    ]
    for outcome in ReviewOutcome:
        probes.append(
            (
                COMPLETE,
                lambda outcome=outcome: complete_item(
                    actor,
                    snapshot,
                    expected_version=snapshot.version,
                    current_findings=loaded.current_findings,
                    evaluation=loaded.evaluation,
                    outcome=outcome,
                    reason="Actual review reason",
                ),
            )
        )
    result = set()
    for name, probe in probes:
        try:
            probe()
        except ServiceError:
            pass
        else:
            result.add(name)
    return sorted(result)


@pytest.mark.parametrize("actor", [ADMIN, INSPECTOR, OTHER_INSPECTOR, DISPATCHER, REMEDIATOR])
@pytest.mark.parametrize("status", list(ItemStatus))
@pytest.mark.parametrize("parent", ["in_progress", "completed", "cancelled"])
def test_projection_matches_command_guards_for_states_roles_and_parent(actor, status, parent):
    snapshot, loaded = item(status=status, inspection_status=parent), context()
    before = (replace(snapshot), replace(loaded))
    assert allowed_item_actions(actor, snapshot, loaded, enabled=ITEM_OPERATIONS) == direct_actions(
        actor, snapshot, loaded
    )
    assert (snapshot, loaded) == before


@pytest.mark.parametrize(
    "status,expected",
    [
        ("uploaded", [SUBMIT]),
        ("failed", [RETRY]),
        ("needs_review", sorted([SUBMIT, COMPLETE])),
        ("draft", []),
    ],
)
def test_positive_probes_have_real_expected_actions(status, expected):
    assert (
        allowed_item_actions(INSPECTOR, item(status=status), context(), enabled=ITEM_OPERATIONS)
        == expected
    )


@pytest.mark.parametrize(
    "missing,operation,status",
    [
        ("current_findings", SUBMIT, "uploaded"),
        ("images", SUBMIT, "uploaded"),
        ("selections", SUBMIT, "uploaded"),
        ("current_findings", RETRY, "failed"),
        ("images", RETRY, "failed"),
        ("old_run", RETRY, "failed"),
        ("current_findings", COMPLETE, "needs_review"),
        ("evaluation", COMPLETE, "needs_review"),
    ],
)
def test_unloaded_context_is_not_an_empty_or_safe_result(missing, operation, status):
    assert (
        allowed_item_actions(
            INSPECTOR,
            item(status=status),
            context(**{missing: None}),
            enabled=frozenset({operation}),
        )
        == []
    )


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"has_unknown": True},
        {"insufficient_facts": True},
        {"has_unknown": True, "insufficient_facts": True},
        {"has_unknown": "false"},
        {"insufficient_facts": None},
        {"id": "old-evaluation"},
        {"run_id": "old-run"},
        {"status": "failed"},
    ],
)
@pytest.mark.parametrize(
    "findings",
    [
        (),
        (finding(),),
        (finding(status="confirmed"),),
        (finding(status="rejected"),),
        (finding(status="cannot_determine"),),
        (finding(superseded=True),),
        (finding(run_id="old-run"),),
        (finding(item_id="other-item"),),
    ],
)
def test_complete_probes_preserve_unknown_and_finding_aggregation(flags, findings):
    snapshot, loaded = item(), context(evaluation=evaluation(**flags), current_findings=findings)
    expected = [name for name in direct_actions(INSPECTOR, snapshot, loaded) if name == COMPLETE]
    assert (
        allowed_item_actions(INSPECTOR, snapshot, loaded, enabled=frozenset({COMPLETE})) == expected
    )


@pytest.mark.parametrize(
    "operation,status,field,value",
    [
        (SUBMIT, "uploaded", "images", ()),
        (SUBMIT, "uploaded", "images", (image("inspection_item", "other-item"),)),
        (SUBMIT, "uploaded", "selections", ()),
        (SUBMIT, "uploaded", "current_findings", (finding(status="confirmed"),)),
        (RETRY, "failed", "images", ()),
        (RETRY, "failed", "old_run", failed_run(configuration_available=False)),
        (RETRY, "failed", "old_run", failed_run(id="old-run")),
        (RETRY, "failed", "current_findings", (finding(status="confirmed"),)),
    ],
)
def test_invalid_command_context_does_not_advertise_action(operation, status, field, value):
    assert (
        allowed_item_actions(
            INSPECTOR,
            item(status=status),
            context(**{field: value}),
            enabled=frozenset({operation}),
        )
        == []
    )


def test_unimplemented_capabilities_never_call_command_guards():
    with (
        patch.object(actions, "submit_item") as submit,
        patch.object(actions, "retry_item") as retry,
        patch.object(actions, "complete_item") as complete,
    ):
        assert allowed_item_actions(INSPECTOR, item(), context(), enabled=frozenset()) == []
    for guard in (submit, retry, complete):
        guard.assert_not_called()


def test_unknown_capability_is_configuration_error_even_with_unloaded_context():
    with pytest.raises(ValueError, match="Unrecognized"):
        allowed_item_actions(INSPECTOR, item(), ItemActionContext(), enabled=frozenset({"typo"}))


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("broken loader"),
        ServiceError("DEPENDENCY_UNAVAILABLE", 503, "dependency"),
        ServiceError("UNAUTHENTICATED", 401, "auth"),
    ],
)
def test_faults_are_not_disguised_as_unavailable_actions(error):
    with patch.object(actions, "submit_item", side_effect=error):
        with pytest.raises(type(error)) as caught:
            allowed_item_actions(
                INSPECTOR, item(status="uploaded"), context(), enabled=frozenset({SUBMIT})
            )
    assert caught.value is error


@pytest.mark.parametrize(
    "actor",
    [
        replace(INSPECTOR, tenant_id="other"),
        replace(INSPECTOR, roles=()),
        replace(INSPECTOR, roles=(("viewer", "laboratory", "other"),)),
    ],
)
def test_projection_itself_requires_visibility_even_without_capabilities(actor):
    with pytest.raises(ServiceError) as caught:
        allowed_item_actions(actor, item(), ItemActionContext(), enabled=frozenset())
    assert caught.value.status == 404
