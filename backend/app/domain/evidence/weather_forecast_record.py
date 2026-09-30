from typing import Literal, Self

from pydantic import model_validator

from app.domain.common import CanonicalDecimal, NonEmptyString, UtcDatetime
from app.domain.evidence.source_record_base import _SourceRecordBase


class WeatherForecastRecord(_SourceRecordBase):
    record_type: Literal["weather_forecast"] = "weather_forecast"
    grid_id: NonEmptyString
    issued_at: UtcDatetime
    weather_variable: NonEmptyString
    value: CanonicalDecimal

    @model_validator(mode="after")
    def validate_forecast(self) -> Self:
        if self.valid_time_range is None:
            raise ValueError("weather forecast requires valid_time_range")
        if self.issued_at > self.valid_time_range.end:
            raise ValueError("issued_at must not be after forecast validity")
        if self.raw_unit is None:
            raise ValueError("weather forecast requires raw_unit")
        return self
