from typing import Self

from pydantic import StrictBool, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.evidence_aggregation_method import (
    EvidenceAggregationMethod,
)


class AggregationProfile(DomainModel):
    profile_id: NonEmptyString
    extends_profile_id: NonEmptyString | None = None
    allowed_methods: tuple[EvidenceAggregationMethod, ...]
    default_method: EvidenceAggregationMethod
    allow_partial_coverage: StrictBool

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if not self.allowed_methods:
            raise ValueError("aggregation profile must allow at least one method")
        ensure_unique_tuple(self.allowed_methods, field_name="allowed_methods")
        if self.default_method not in self.allowed_methods:
            raise ValueError("aggregation default must be an allowed method")
        return self
