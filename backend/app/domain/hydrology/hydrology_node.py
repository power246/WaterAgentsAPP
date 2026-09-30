from typing import Self

from pydantic import Field, field_validator, model_validator

from app.domain.common import (
    DomainFloat,
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.hydrology._topology_validation import (
    comid_key,
    coverage_key,
    validate_comid,
)
from app.domain.hydrology.coverage_unit import CoverageUnit
from app.domain.hydrology.hydrology_exclusion_reason import (
    HydrologyExclusionReason,
)
from app.domain.hydrology.hydrology_participation import HydrologyParticipation
from app.domain.hydrology.location import GeoPoint
from app.domain.hydrology.topology_station_ref import TopologyStationRef
from app.domain.hydrology.waterbody_geometry import WaterbodyGeometrySummary


class HydrologyNode(DomainModel):
    comid: NonEmptyString
    point: GeoPoint | None
    geometry_ref: NonEmptyString | None = None
    geometry_summary: WaterbodyGeometrySummary | None = None
    depth: int = Field(strict=True, ge=0)
    cumulative_distance_km: DomainFloat = Field(ge=0)
    is_target: bool
    is_inlet: bool
    participation: HydrologyParticipation
    qualified_units: tuple[CoverageUnit, ...] = ()
    station_refs: tuple[TopologyStationRef, ...] = ()
    excluded_reasons: tuple[HydrologyExclusionReason, ...] = ()

    @field_validator("comid")
    @classmethod
    def validate_comid(cls, value: str) -> str:
        return validate_comid(value)

    @model_validator(mode="after")
    def validate_details(self) -> Self:
        if (self.geometry_ref is None) != (self.geometry_summary is None):
            raise ValueError("node geometry ref and summary must be provided together")
        ensure_unique_tuple(self.qualified_units, field_name="qualified_units")
        if self.qualified_units != tuple(
            sorted(self.qualified_units, key=coverage_key)
        ):
            raise ValueError("qualified_units must be sorted")
        ensure_unique_tuple(self.station_refs, field_name="station_refs")
        expected_station_refs = tuple(
            sorted(
                self.station_refs,
                key=lambda item: (item.provider.value, item.station_id),
            )
        )
        if self.station_refs != expected_station_refs:
            raise ValueError("station_refs must be sorted")
        ensure_unique_tuple(self.excluded_reasons, field_name="excluded_reasons")
        expected_reasons = tuple(
            sorted(self.excluded_reasons, key=_exclusion_key)
        )
        if self.excluded_reasons != expected_reasons:
            raise ValueError("excluded_reasons must be sorted")
        if self.is_inlet != bool(self.qualified_units):
            raise ValueError("inlet identity must match qualified_units")
        if self.is_inlet != bool(self.station_refs):
            raise ValueError("inlet identity must match station_refs")
        if self.participation is HydrologyParticipation.ACTIVE:
            if self.excluded_reasons:
                raise ValueError("active nodes must not have excluded reasons")
        elif not self.excluded_reasons:
            raise ValueError("excluded nodes require structured reasons")
        return self


def _exclusion_key(
    reason: HydrologyExclusionReason,
) -> tuple[object, ...]:
    return (
        reason.code.value,
        *comid_key(reason.branch_root_comid),
        coverage_key(reason.coverage_unit),
        *comid_key(reason.stop_comid),
        *_optional_comid_key(reason.blocked_upstream_comid),
    )


def _optional_comid_key(comid: str | None) -> tuple[int, str]:
    return (-1, "") if comid is None else comid_key(comid)
