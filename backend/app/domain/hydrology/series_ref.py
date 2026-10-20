from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.availability_coverage_source import (
    AvailabilityCoverageSource,
)
from app.domain.hydrology.coverage_unit import CoverageUnit
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.parameter_provider import ParameterProvider


class SeriesRef(DomainModel):
    provider: ParameterProvider
    station_id: NonEmptyString
    parameter: ParameterKey
    channel: NonEmptyString
    series_identity: NonEmptyString
    coverage_source: AvailabilityCoverageSource
    covered_units: tuple[CoverageUnit, ...]

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if not self.covered_units:
            raise ValueError("covered_units must not be empty")
        ensure_unique_tuple(self.covered_units, field_name="covered_units")
        if any(unit.parameter != self.parameter for unit in self.covered_units):
            raise ValueError("series reference units must use its parameter")
        expected_order = tuple(
            sorted(
                self.covered_units,
                key=lambda unit: (
                    unit.segment_index,
                    -1 if unit.window_index is None else unit.window_index,
                ),
            )
        )
        if self.covered_units != expected_order:
            raise ValueError("series reference units must be sorted")
        time_series_source = self.coverage_source in {
            AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_ENVELOPE,
            AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_OBSERVATION,
            AvailabilityCoverageSource.PREDICTION_TIME_SERIES_BASELINE,
        }
        if time_series_source != (
            self.provider is ParameterProvider.WDFN_TIME_SERIES
        ):
            raise ValueError("coverage source must match the series provider")
        if (
            self.provider is ParameterProvider.WDFN_TIME_SERIES
            and self.channel not in {"continuous", "daily"}
        ):
            raise ValueError("time-series channel must match provider metadata")
        if (
            self.provider is ParameterProvider.USGS_SAMPLES
            and self.channel != "discrete_samples"
        ):
            raise ValueError("sample channel must be discrete_samples")
        return self
