from pydantic import field_validator

from app.domain.attribution.attribution_basis import AttributionBasis
from app.domain.attribution.attribution_direction import AttributionDirection
from app.domain.attribution.attribution_magnitude import AttributionMagnitude
from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple


class AttributionFactor(DomainModel):
    factor: NonEmptyString
    direction: AttributionDirection
    magnitude: AttributionMagnitude
    basis: AttributionBasis
    explanation: NonEmptyString
    evidence_refs: tuple[NonEmptyString, ...]

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value:
            raise ValueError("evidence_refs must not be empty")
        return ensure_unique_tuple(value, field_name="evidence_refs")
