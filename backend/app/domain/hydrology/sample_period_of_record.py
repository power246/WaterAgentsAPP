from datetime import date
from typing import Self

from pydantic import Field, model_validator

from app.domain.common import DomainModel, NonEmptyString


class SamplePeriodOfRecord(DomainModel):
    station_id: NonEmptyString
    characteristic: NonEmptyString
    first_activity: date
    most_recent_activity: date
    result_count: int = Field(ge=0)
    activity_count: int = Field(ge=0)
    provider_identity: NonEmptyString

    @model_validator(mode="after")
    def validate_period(self) -> Self:
        if self.first_activity > self.most_recent_activity:
            raise ValueError("sample first activity must not be after most recent activity")
        return self
