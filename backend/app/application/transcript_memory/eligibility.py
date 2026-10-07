from dataclasses import dataclass
from uuid import UUID

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
    SessionSummary,
)
from app.application.transcript_memory.source_models import ConfirmedSourceReference
from app.application.transcript_memory.source_reader import TypedRAGSourceItem
from app.domain.common import DomainEnum, DomainModel
from app.domain.evidence.collection_fact_payload import CollectionFactPayload
from app.domain.evidence.collection_status import CollectionStatus
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.historical_inference_payload import HistoricalInferencePayload
from app.domain.inference.inference_result import InferenceResult


class FinalEligibilityReason(DomainEnum):
    ELIGIBLE = "eligible"
    CONTENT_NOT_ALLOWED = "content_not_allowed"
    LIFECYCLE_NOT_COMPLETED = "lifecycle_not_completed"
    PERSISTENCE_UNCONFIRMED = "persistence_unconfirmed"
    CANDIDATE_NOT_MARKED = "candidate_not_marked"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    REFERENCE_CLOSURE_INVALID = "reference_closure_invalid"
    FRAGMENT_NOT_DIRECTLY_ASSOCIATED = "fragment_not_directly_associated"
    FRAGMENT_POLICY_REJECTED = "fragment_policy_rejected"


class FinalEligibilityDecision(DomainModel):
    eligible: bool
    reason: FinalEligibilityReason


@dataclass(frozen=True, slots=True)
class FinalEligibilityContext:
    run_status: RunStatus
    session: SessionSummary
    persistence_confirmed: bool
    source: TypedRAGSourceItem[DomainModel]
    confirmed_references: tuple[ConfirmedSourceReference, ...]


def evaluate_final_result_eligibility(
    context: FinalEligibilityContext,
) -> FinalEligibilityDecision:
    common = _evaluate_common(context)
    if common is not None:
        return common
    source = context.source
    result = source.body
    if (
        source.item.content_type != "inference_result"
        or source.item.content_schema_version != 1
        or not isinstance(result, InferenceResult)
    ):
        return _denied(FinalEligibilityReason.CONTENT_NOT_ALLOWED)
    session = context.session
    if (
        result.run_id != source.item.run_id
        or result.session_id != source.item.session_id
        or result.session_id != session.session_id
        or result.target_comid != session.target_comid
        or session.result_ref is None
        or session.result_ref.run_id != source.item.run_id
        or session.result_ref.item_id != source.item.item_id
    ):
        return _denied(FinalEligibilityReason.OWNERSHIP_MISMATCH)
    return _eligible()


def evaluate_final_fragment_eligibility(
    context: FinalEligibilityContext,
    *,
    root: TypedRAGSourceItem[InferenceResult],
) -> FinalEligibilityDecision:
    common = _evaluate_common(context)
    if common is not None:
        return common
    source = context.source
    fragment = source.body
    if (
        source.item.content_type != "evidence_fragment"
        or source.item.content_schema_version != 1
        or not isinstance(fragment, EvidenceFragment)
    ):
        return _denied(FinalEligibilityReason.CONTENT_NOT_ALLOWED)
    if (
        not fragment.rag_eligible
        or fragment.collection_status is not CollectionStatus.AVAILABLE
        or fragment.quality_class
        not in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
        or isinstance(fragment.payload, (CollectionFactPayload, HistoricalInferencePayload))
        or (fragment.derivation is not None and not fragment.derivation.deterministic)
    ):
        return _denied(FinalEligibilityReason.FRAGMENT_POLICY_REJECTED)
    result = root.body
    if (
        source.item.run_id != root.item.run_id
        or source.item.session_id != root.item.session_id
        or fragment.target_comid != result.target_comid
    ):
        return _denied(FinalEligibilityReason.OWNERSHIP_MISMATCH)
    referenced_evidence = {
        evidence_id
        for series in result.series
        for point in series.points
        for evidence_id in point.evidence_refs
    }
    if (
        source.item.item_id not in root.references
        or fragment.evidence_id not in referenced_evidence
    ):
        return _denied(FinalEligibilityReason.FRAGMENT_NOT_DIRECTLY_ASSOCIATED)
    return _eligible()


def _evaluate_common(
    context: FinalEligibilityContext,
) -> FinalEligibilityDecision | None:
    source = context.source
    session = context.session
    if (
        context.run_status is not RunStatus.COMPLETED
        or session.status is not AgentSessionStatus.COMPLETED
        or session.agent_role is not AgentRole.WATERBODY
    ):
        return _denied(FinalEligibilityReason.LIFECYCLE_NOT_COMPLETED)
    if not context.persistence_confirmed:
        return _denied(FinalEligibilityReason.PERSISTENCE_UNCONFIRMED)
    if not source.item.rag_eligible:
        return _denied(FinalEligibilityReason.CANDIDATE_NOT_MARKED)
    if session.result_ref is None or source.item.run_id != session.result_ref.run_id:
        return _denied(FinalEligibilityReason.OWNERSHIP_MISMATCH)
    if not _reference_closure_is_confirmed(
        source.item.run_id,
        source.item.session_id,
        source.references,
        context.confirmed_references,
    ):
        return _denied(FinalEligibilityReason.REFERENCE_CLOSURE_INVALID)
    return None


def _reference_closure_is_confirmed(
    run_id: UUID,
    session_id: UUID | None,
    references: tuple[UUID, ...],
    confirmed: tuple[ConfirmedSourceReference, ...],
) -> bool:
    by_id = {reference.item_id: reference for reference in confirmed}
    if len(by_id) != len(confirmed):
        return False
    for item_id in references:
        reference = by_id.get(item_id)
        if (
            reference is None
            or not reference.persistence_confirmed
            or reference.run_id != run_id
            or reference.content_schema_version <= 0
            or not reference.content_type.strip()
            or reference.session_id not in {None, session_id}
        ):
            return False
    return True


def _eligible() -> FinalEligibilityDecision:
    return FinalEligibilityDecision(
        eligible=True,
        reason=FinalEligibilityReason.ELIGIBLE,
    )


def _denied(reason: FinalEligibilityReason) -> FinalEligibilityDecision:
    return FinalEligibilityDecision(eligible=False, reason=reason)
