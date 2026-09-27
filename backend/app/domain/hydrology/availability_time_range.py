from typing import Self

from pydantic import Field, model_validator

from app.domain.common import DomainModel, UtcDatetime


class AvailabilityTimeRange(DomainModel):
    segment_index: int = Field(strict=True, ge=0)
    start: UtcDatetime
    end: UtcDatetime

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.start >= self.end:
            raise ValueError("availability range start must be before end")
        return self
