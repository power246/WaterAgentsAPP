from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.application.evidence_tools.scope import EvidenceSessionScope
from app.domain import (
    EvidenceFragment,
    EvidenceFragmentReference,
    ItemReference,
    NumericPlanTask,
)
from app.domain.evidence.measured_anchor import MeasuredAnchor


class NumericEstimationError(ValueError):
    def __init__(self, code: str) -> None:
        if code not in {
            "numeric_estimation_call_budget_exceeded",
            "numeric_estimation_context_budget_exceeded",
            "numeric_estimation_deadline_exceeded",
            "numeric_estimation_model_circuit_open",
        }:
            raise ValueError("unknown numeric estimation error code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class NumericEstimateRequest:
    scope: EvidenceSessionScope
    task: NumericPlanTask
    plan_ref: ItemReference
    input_refs: tuple[EvidenceFragmentReference, ...]
    input_fragments: tuple[EvidenceFragment, ...]
    context_refs: tuple[EvidenceFragmentReference, ...]
    context_fragments: tuple[EvidenceFragment, ...]
    measured_anchors: tuple[MeasuredAnchor, ...]


@dataclass(frozen=True, slots=True)
class NumericEstimateResult:
    value: str | None
    value_lower: str | None
    value_upper: str | None
    confidence: float
    rationale: str
    model_provider: str
    model_name: str
    operation_id: str
    prompt_digest: str


NumericEstimator = Callable[[NumericEstimateRequest], Awaitable[NumericEstimateResult]]
