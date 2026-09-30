from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import NonEmptyString, ensure_unique_tuple
from app.domain.evidence.source_record_base import _SourceRecordBase


class FacilityRecord(_SourceRecordBase):
    record_type: Literal["facility"] = "facility"
    registry_id: NonEmptyString
    facility_id: NonEmptyString
    permit_id: NonEmptyString | None = None
    program: NonEmptyString
    status: NonEmptyString
    public_attributes: tuple[tuple[NonEmptyString, NonEmptyString], ...] = ()

    @model_validator(mode="after")
    def validate_facility(self) -> Self:
        if self.location.point is None:
            raise ValueError("facility requires a point location")
        if self.raw_unit is not None:
            raise ValueError("facility must not declare raw_unit")
        names = tuple(name for name, _ in self.public_attributes)
        ensure_unique_tuple(names, field_name="facility public attribute names")
        return self
