from datetime import datetime
from typing import Self

from pydantic import model_validator

from app.domain.common import DomainModel, UtcDatetime, normalize_utc_datetime


class TimeRange(DomainModel):
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self

    def contains(self, instant: datetime) -> bool:
        instant_utc = normalize_utc_datetime(instant)
        return self.start <= instant_utc < self.end

    def overlaps(self, other: "TimeRange") -> bool:
        return self.start < other.end and other.start < self.end
