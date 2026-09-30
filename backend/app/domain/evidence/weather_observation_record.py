from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from app.domain.common import CanonicalDecimal, NonEmptyString
from app.domain.evidence.source_record_base import _SourceRecordBase


class WeatherObservationRecord(_SourceRecordBase):
    record_type: Literal["weather_observation"] = "weather_observation"
    grid_id: NonEmptyString | None = None
    weather_variable: NonEmptyString
    value: CanonicalDecimal
    duration_seconds: Annotated[int, Field(strict=True, ge=0)] | None = None
    statistic: NonEmptyString

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.observed_time is None:
            raise ValueError("weather observation requires observed_time")
        if self.location.station_id is None and self.grid_id is None:
            raise ValueError("weather observation requires station_id or grid_id")
        if self.raw_unit is None:
            raise ValueError("weather observation requires raw_unit")
        return self
