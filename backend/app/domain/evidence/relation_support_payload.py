from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference


class RelationSupportPayload(DomainModel):
    payload_type: Literal["relation_support"] = "relation_support"
    rule_id: NonEmptyString
    plan_digest: NonEmptyString
    input_evidence_refs: tuple[EvidenceFragmentReference, ...]
    inference_strength: Literal["directional"] = "directional"
    rationale: NonEmptyString

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        if not self.input_evidence_refs:
            raise ValueError("relation support requires input evidence")
        ensure_unique_tuple(
            self.input_evidence_refs,
            field_name="relation support input evidence",
        )
        return self
