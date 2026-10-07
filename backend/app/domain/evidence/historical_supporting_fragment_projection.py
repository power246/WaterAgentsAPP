from typing import Literal

from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence.evidence_applicability import EvidenceApplicability
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.historical_supporting_fragment_reference import (
    HistoricalSupportingFragmentReference,
)
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.time.time_range import TimeRange


class HistoricalSupportingFragmentProjection(DomainModel):
    original_item_id: NonEmptyString
    evidence_id: NonEmptyString
    parameter: ParameterKey
    valid_time_range: TimeRange
    quality_class: Literal[
        EvidenceQualityClass.VALID,
        EvidenceQualityClass.SUSPICIOUS,
    ]
    applicability: Literal[EvidenceApplicability.CONTEXTUAL_ONLY] = (
        EvidenceApplicability.CONTEXTUAL_ONLY
    )

    @property
    def reference(self) -> HistoricalSupportingFragmentReference:
        return HistoricalSupportingFragmentReference(
            item_id=self.original_item_id,
            evidence_id=self.evidence_id,
        )
