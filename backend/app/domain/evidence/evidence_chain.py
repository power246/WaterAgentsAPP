from pydantic import field_validator

from app.domain.common import DomainModel, ensure_unique_tuple
from app.domain.evidence.evidence_reference import EvidenceReference


class EvidenceChain(DomainModel):
    references: tuple[EvidenceReference, ...]

    @field_validator("references")
    @classmethod
    def validate_references(
        cls,
        value: tuple[EvidenceReference, ...],
    ) -> tuple[EvidenceReference, ...]:
        if not value:
            raise ValueError("references must not be empty")
        evidence_ids = tuple(reference.evidence_id for reference in value)
        ensure_unique_tuple(evidence_ids, field_name="reference evidence IDs")
        return value
