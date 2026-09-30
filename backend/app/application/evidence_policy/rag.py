from app.domain.evidence.collection_fact_payload import CollectionFactPayload
from app.domain.evidence.collection_status import CollectionStatus
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass


def is_rag_candidate(
    fragment: object,
    *,
    operation_complete: bool,
    reference_complete: bool,
    redacted: bool,
) -> bool:
    if (
        not isinstance(fragment, EvidenceFragment)
        or not operation_complete
        or not reference_complete
        or not redacted
        or fragment.collection_status is not CollectionStatus.AVAILABLE
        or fragment.quality_class
        not in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
        or isinstance(fragment.payload, CollectionFactPayload)
        or not fragment.referenced_item_ids()
    ):
        return False
    return fragment.derivation is None or fragment.derivation.deterministic


def apply_rag_policy(
    fragment: EvidenceFragment,
    *,
    operation_complete: bool,
    reference_complete: bool,
    redacted: bool,
) -> EvidenceFragment:
    values = fragment.model_dump(mode="python")
    values["rag_eligible"] = is_rag_candidate(
        fragment,
        operation_complete=operation_complete,
        reference_complete=reference_complete,
        redacted=redacted,
    )
    return EvidenceFragment.model_validate(values)
