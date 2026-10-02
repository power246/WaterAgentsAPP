from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import NonEmptyString, ensure_unique_tuple
from app.domain.evidence.source_record_base import _SourceRecordBase


class FacilityRecord(_SourceRecordBase):
    record_type: Literal["facility"] = "facility"
    applicability: Literal["contextual_only"] = "contextual_only"
    spatially_in_scope: bool = False
    registry_id: NonEmptyString
    facility_id: NonEmptyString
    permit_id: NonEmptyString | None = None
    program: NonEmptyString
    status: NonEmptyString
    public_attributes: tuple[tuple[NonEmptyString, NonEmptyString], ...] = ()

    @model_validator(mode="after")
    def validate_facility(self) -> Self:
        if (
            self.location.point is None
            and self.location.provider_location_id is None
        ):
            raise ValueError(
                "facility without coordinates requires provider_location_id"
            )
        if self.spatially_in_scope and self.location.point is None:
            raise ValueError(
                "facility cannot be spatially in scope without coordinates"
            )
        if self.spatially_in_scope != (
            self.provenance.facility_final_distance_km is not None
        ):
            raise ValueError(
                "facility spatial scope must match final distance provenance"
            )
        if (
            self.location.distance_to_target_km
            != self.provenance.facility_final_distance_km
        ):
            raise ValueError("facility location and provenance distances must match")
        if self.raw_unit is not None:
            raise ValueError("facility must not declare raw_unit")
        names = tuple(name for name, _ in self.public_attributes)
        ensure_unique_tuple(names, field_name="facility public attribute names")
        return self
