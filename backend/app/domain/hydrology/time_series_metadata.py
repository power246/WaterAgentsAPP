from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.time_series_kind import TimeSeriesKind


class TimeSeriesMetadata(DomainModel):
    series_id: NonEmptyString
    station_id: NonEmptyString
    parameter: ParameterKey
    unit: NonEmptyString
    begin: UtcDatetime
    end: UtcDatetime
    kind: TimeSeriesKind
    provider_identity: NonEmptyString

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.begin > self.end:
            raise ValueError("time-series begin must not be after end")
        return self
