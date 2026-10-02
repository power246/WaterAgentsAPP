from typing import Self

from pydantic import Field, model_validator

from app.domain.common import (
    DomainFloat,
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.hydrology.location import GeoPoint
from app.domain.hydrology.waterbody_geometry import (
    WaterbodyGeometry,
    WaterbodyGeometrySummary,
)


class RiverSegment(DomainModel):
    comid: NonEmptyString
    location: GeoPoint | None
    geometry_ref: NonEmptyString | None = None
    geometry_summary: WaterbodyGeometrySummary | None = None
    geometry: WaterbodyGeometry | None = None
    distance_to_downstream_km: DomainFloat | None = Field(default=None, gt=0)
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
        geometry_fields = (
            self.geometry_ref,
            self.geometry_summary,
            self.geometry,
        )
        if all(item is None for item in geometry_fields):
            return self
        if any(item is None for item in geometry_fields):
            raise ValueError("segment geometry ref, summary, and content must agree")
        assert self.geometry is not None
        if self.geometry.source_comid != self.comid:
            raise ValueError("segment geometry must match its COMID")
        if self.geometry_ref != self.geometry.geometry_ref:
            raise ValueError("segment geometry_ref must match geometry content")
        if self.geometry_summary != self.geometry.summary():
            raise ValueError("segment geometry_summary must match geometry content")
        if self.location != self.geometry.representative_point:
            raise ValueError("segment location must match geometry representative point")
        return self
