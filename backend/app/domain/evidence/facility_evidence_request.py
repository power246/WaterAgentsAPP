from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import DomainFloat, DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.location import GeoPoint


class FacilityEvidenceRequest(DomainModel):
    operation_id: NonEmptyString
    geometry_ref: NonEmptyString
    exact_radius_miles: Annotated[DomainFloat, Field(ge=1, le=20)]
    provider_radius_miles: Annotated[DomainFloat, Field(ge=1, le=20)]
    sample_centers: tuple[GeoPoint, ...]
    affected_window_ids: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.provider_radius_miles < self.exact_radius_miles:
            raise ValueError("provider radius must cover the exact radius")
        if not self.sample_centers or len(self.sample_centers) > 32:
            raise ValueError("sample_centers must contain between 1 and 32 points")
        ensure_unique_tuple(self.sample_centers, field_name="sample_centers")
        if not self.affected_window_ids:
            raise ValueError("affected_window_ids must not be empty")
        ensure_unique_tuple(
            self.affected_window_ids,
            field_name="affected_window_ids",
        )
        return self
