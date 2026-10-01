from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import DomainFloat, DomainModel, NonEmptyString
from app.domain.hydrology.location import GeoPoint


class EvidenceLocation(DomainModel):
    point: GeoPoint | None
    comid: NonEmptyString | None
    station_id: NonEmptyString | None
    provider_location_id: NonEmptyString | None = None
    distance_to_target_km: Annotated[DomainFloat, Field(ge=0)] | None

    @model_validator(mode="after")
    def require_location_identity(self) -> Self:
        if (
            self.point is None
            and self.comid is None
            and self.station_id is None
            and self.provider_location_id is None
        ):
            raise ValueError(
                "point, comid, station_id, or provider_location_id is required"
            )
        return self
