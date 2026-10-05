"""Values and deterministic error policy for rule_evaluation tasks."""

from dataclasses import dataclass


class RuleLeaseLost(RuntimeError):
    def __init__(self):
        super().__init__("LEASE_LOST")


RULE_RETRYABLE = frozenset({"DEPENDENCY_UNAVAILABLE", "STAGE_TIMEOUT", "INTERNAL_ERROR"})


@dataclass(frozen=True)
class RuleInput:
    tenant_id: str
    item_id: str
    run_id: str
    fact_revision_id: str
    evaluation_id: str
    rule_bundle_id: str
    reference_date: str
    facts: dict
    bundle: dict
    laboratory_id: str | None = None
    bundle_checksum: str | None = None


@dataclass(frozen=True)
class RuleLease:
    tenant_id: str
    task_id: str
    owner: str
    attempt_id: str
    token: int
    generation: int
    attempt: int
    input: RuleInput
