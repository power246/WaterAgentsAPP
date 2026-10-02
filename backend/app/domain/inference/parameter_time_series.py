from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import (
    DomainFloat,
    DomainModel,
    NonEmptyString,
    ensure_unique_tuple,
)
from app.domain.hydrology.water_quality_parameter import WaterQualityParameter
from app.domain.inference.indeterminate_reason import IndeterminateReason
from app.domain.inference.time_window_result import TimeWindowResult
from app.domain.time.time_resolution import DEFAULT_TIME_RESOLUTION, TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource


class ParameterTimeSeries(DomainModel):
    parameter: WaterQualityParameter
    time_resolution: TimeResolution = DEFAULT_TIME_RESOLUTION
    time_resolution_source: TimeResolutionSource
    points: tuple[TimeWindowResult, ...]
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    confidence_rationale: NonEmptyString
    contributing_cell_refs: tuple[NonEmptyString, ...]

    @model_validator(mode="after")
    def validate_points(self) -> Self:
        if not self.points:
            raise ValueError("points must not be empty")
        cell_ids = tuple(point.cell_id for point in self.points)
        ensure_unique_tuple(cell_ids, field_name="point cell IDs")
        for previous, current in zip(self.points, self.points[1:]):
            if current.start_time <= previous.start_time:
                raise ValueError("points must be ordered by strictly increasing start_time")
            if current.start_time < previous.end_time:
                raise ValueError("points must not overlap")
        ensure_unique_tuple(
            self.contributing_cell_refs,
            field_name="contributing_cell_refs",
        )
        if self.contributing_cell_refs != cell_ids:
            raise ValueError(
                "contributing_cell_refs must exactly match ordered point cell IDs"
            )
        if all(
            IndeterminateReason.INSUFFICIENT_EVIDENCE
            in point.assessment.reason_codes
            for point in self.points
        ) and self.confidence != 0:
            raise ValueError("fixed indeterminate series requires confidence 0")
        return self
