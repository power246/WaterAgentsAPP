from typing import Literal

from app.domain.common import CanonicalDecimal, DomainModel, NonEmptyString
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference


class ConversionPayload(DomainModel):
    payload_type: Literal["conversion"] = "conversion"
    value: CanonicalDecimal
    unit: NonEmptyString
    input_evidence_ref: EvidenceFragmentReference
    conversion_id: NonEmptyString
