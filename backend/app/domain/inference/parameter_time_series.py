from typing import Annotated, Self

from pydantic import Field, model_validator

from app.domain.common import DomainModel
from app.domain.hydrology.water_quality_parameter import WaterQualityParameter
from app.domain.inference.time_window_result import TimeWindowResult
from app.domain.time.time_resolution import DEFAULT_TIME_RESOLUTION, TimeResolution
from app.domain.time.time_resolution_source import TimeResolutionSource


class ParameterTimeSeries(DomainModel):
    parameter: WaterQualityParameter
    time_resolution: TimeResolution = DEFAULT_TIME_RESOLUTION
    time_resolution_source: TimeResolutionSource
    points: tuple[TimeWindowResult, ...]
    confidence: Annotated[float, Field(ge=0, le=1)]

    @model_validator(mode="after")
    def validate_points(self) -> Self:
        if not self.points:
            raise ValueError("points must not be empty")
        for previous, current in zip(self.points, self.points[1:]):
            if current.start_time <= previous.start_time:
                raise ValueError("points must be ordered by strictly increasing start_time")
            if current.start_time < previous.end_time:
                raise ValueError("points must not overlap")
        return self
