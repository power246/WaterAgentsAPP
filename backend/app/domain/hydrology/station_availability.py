from typing import Self

from pydantic import field_validator, model_validator

from app.domain.common import (
    DomainModel,
    NonEmptyString,
    UtcDatetime,
    ensure_unique_tuple,
)
from app.domain.hydrology.coverage_unit import CoverageUnit
from app.domain.hydrology.monitoring_station import MonitoringStation
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.hydrology.series_ref import SeriesRef


class StationAvailability(DomainModel):
    station: MonitoringStation
    comid: NonEmptyString
    providers: tuple[ParameterProvider, ...]
    covered_units: tuple[CoverageUnit, ...]
    series_refs: tuple[SeriesRef, ...]
    checked_at: UtcDatetime

    @field_validator("comid")
    @classmethod
    def validate_comid(cls, value: str) -> str:
        if not value.isdecimal() or value.startswith("0"):
            raise ValueError("comid must be a positive decimal string")
        return value

    @model_validator(mode="after")
    def validate_qualification(self) -> Self:
        if self.station.associated_comid != self.comid:
            raise ValueError("station must have an explicit hydrologic association")
        if not self.series_refs or not self.covered_units or not self.providers:
            raise ValueError("station availability must contain qualified metadata")
        ensure_unique_tuple(self.series_refs, field_name="series_refs")
        ensure_unique_tuple(self.covered_units, field_name="covered_units")
        ensure_unique_tuple(self.providers, field_name="providers")
        if any(
            reference.station_id != self.station.station_id
            for reference in self.series_refs
        ):
            raise ValueError("series references must belong to the station")
        expected_units = tuple(
            sorted(
                {
                    unit
                    for reference in self.series_refs
                    for unit in reference.covered_units
                },
                key=lambda unit: (
                    unit.segment_index,
                    unit.parameter.code_system,
                    unit.parameter.code,
                ),
            )
        )
        if self.covered_units != expected_units:
            raise ValueError("covered_units must be the sorted series-ref union")
        expected_providers = tuple(
            sorted(
                {reference.provider for reference in self.series_refs},
                key=lambda provider: provider.value,
            )
        )
        if self.providers != expected_providers:
            raise ValueError("providers must be the sorted series-ref provider set")
        expected_refs = tuple(
            sorted(
                self.series_refs,
                key=lambda reference: (
                    reference.provider.value,
                    reference.parameter.code_system,
                    reference.parameter.code,
                    reference.coverage_source.value,
                    reference.series_identity,
                ),
            )
        )
        if self.series_refs != expected_refs:
            raise ValueError("series_refs must be sorted")
        return self
