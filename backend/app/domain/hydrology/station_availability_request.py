from typing import Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.hydrology.availability_time_range import AvailabilityTimeRange
from app.domain.hydrology.coverage_unit import CoverageUnit


class StationAvailabilityRequest(DomainModel):
    comid: NonEmptyString
    coverage_units: tuple[CoverageUnit, ...]
    historical_ranges: tuple[AvailabilityTimeRange, ...]
    prediction_baseline_ranges: tuple[AvailabilityTimeRange, ...]

    @field_validator("comid")
    @classmethod
    def validate_comid(cls, value: str) -> str:
        if not value.isdecimal() or value.startswith("0"):
            raise ValueError("comid must be a positive decimal string")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        if not self.coverage_units:
            raise ValueError("coverage_units must not be empty")
        ensure_unique_tuple(self.coverage_units, field_name="coverage_units")
        expected_units = tuple(
            sorted(
                self.coverage_units,
                key=lambda unit: (
                    unit.segment_index,
                    -1 if unit.window_index is None else unit.window_index,
                    unit.parameter.code_system,
                    unit.parameter.code,
                ),
            )
        )
        if self.coverage_units != expected_units:
            raise ValueError("coverage_units must be sorted")

        ranges = (*self.historical_ranges, *self.prediction_baseline_ranges)
        if not ranges:
            raise ValueError("at least one availability range is required")
        historical_indexes = tuple(
            (item.segment_index, item.window_index) for item in self.historical_ranges
        )
        prediction_indexes = tuple(
            (item.segment_index, item.window_index)
            for item in self.prediction_baseline_ranges
        )
        if any(window_index is None for _, window_index in historical_indexes):
            raise ValueError("historical ranges require window indexes")
        ensure_unique_tuple(
            tuple(window_index for _, window_index in historical_indexes),
            field_name="historical window indexes",
        )
        if any(window_index is not None for _, window_index in prediction_indexes):
            raise ValueError("prediction ranges must use segment indexes")
        ensure_unique_tuple(historical_indexes, field_name="historical_ranges")
        ensure_unique_tuple(
            prediction_indexes,
            field_name="prediction_baseline_ranges",
        )
        if {segment for segment, _ in historical_indexes} & {
            segment for segment, _ in prediction_indexes
        }:
            raise ValueError("availability range kinds must not overlap")
        if historical_indexes != tuple(sorted(historical_indexes)) or (
            prediction_indexes != tuple(sorted(prediction_indexes))
        ):
            raise ValueError("availability ranges must be sorted")
        coverage_indexes = {
            (unit.segment_index, unit.window_index) for unit in self.coverage_units
        }
        if coverage_indexes != set(historical_indexes) | set(prediction_indexes):
            raise ValueError("every coverage segment must have exactly one range kind")
        return self
