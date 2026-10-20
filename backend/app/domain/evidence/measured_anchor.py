from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import (
    DomainFloat,
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.evidence.source_record_reference import SourceRecordReference
from app.domain.hydrology.location import GeoPoint
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.time.time_range import TimeRange


class MeasuredAnchor(DomainModel):
    parameter: ParameterKey
    source_record_refs: tuple[SourceRecordReference, ...]
    origin_comid: NonEmptyString
    station_id: NonEmptyString | None = None
    evidence_time_range: TimeRange
    path_comids: tuple[NonEmptyString, ...]
    distance_km: Annotated[DomainFloat, Field(ge=0)] | None = None
    origin_point: GeoPoint | None = None
    distance_basis: Literal["mapped_channel_estimate"] | None = None

    @model_validator(mode="after")
    def validate_origin(self) -> Self:
        if not self.source_record_refs:
            raise ValueError("measured anchor requires original source record refs")
        ensure_unique_tuple(self.source_record_refs, field_name="anchor source records")
        if not self.path_comids or self.path_comids[0] != self.origin_comid:
            raise ValueError("measured anchor path must begin at its original waterbody")
        ensure_unique_tuple(self.path_comids, field_name="anchor path waterbodies")
        if self.distance_basis is not None and (
            self.distance_km is None or self.origin_point is None
        ):
            raise ValueError("mapped distance requires an origin point and distance")
        return self
