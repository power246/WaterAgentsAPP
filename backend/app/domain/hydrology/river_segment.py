from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.location import GeoPoint


class RiverSegment(DomainModel):
    comid: NonEmptyString
    location: GeoPoint | None
    upstream_comids: tuple[NonEmptyString, ...] = ()
    downstream_comids: tuple[NonEmptyString, ...] = ()

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        ensure_unique_tuple(self.upstream_comids, field_name="upstream_comids")
        ensure_unique_tuple(self.downstream_comids, field_name="downstream_comids")
        if self.comid in self.upstream_comids:
            raise ValueError("comid must not reference itself as upstream")
        if self.comid in self.downstream_comids:
            raise ValueError("comid must not reference itself as downstream")
        return self
