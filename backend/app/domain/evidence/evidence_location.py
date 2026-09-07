from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.hydrology import GeoPoint


class EvidenceLocation(DomainModel):
    point: GeoPoint | None
    comid: NonEmptyString | None
    station_id: NonEmptyString | None
    distance_to_target_km: Annotated[float, Field(ge=0)] | None

    @model_validator(mode="after")
    def require_location_identity(self) -> Self:
        if self.point is None and self.comid is None and self.station_id is None:
            raise ValueError("point, comid, or station_id is required")
        return self
